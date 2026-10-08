"""
services/analytics_service.py

Aggregate figures for the administrator dashboard.

Deliberately simple counts and sums over tables that already exist -- no
materialised views, no rollup tables, no background jobs. At this project's
scale a handful of COUNT/SUM queries is instant, and keeping the numbers derived
live means they can never drift out of sync with the data they describe.

One rule worth stating: the daily series is **gap-filled**. Emitting only days
that have rows would render a chart implying queries stopped on quiet days, a
lie the dashboard would then repeat in a report.
"""

from datetime import date, datetime, timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Plan,
    ReviewSignoff,
    UpgradeRequest,
    UsageRecord,
    User,
)

DEFAULT_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def account_stats(db: Session) -> dict:
    """Headcount, activity and role split."""
    total = int(db.scalar(select(func.count(User.id))) or 0)
    active = int(db.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
    admins = int(db.scalar(
        select(func.count(User.id)).where(User.role == "admin")) or 0)
    month_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = int(db.scalar(
        select(func.count(User.id)).where(User.created_at >= month_start)) or 0)
    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "admins": admins,
        "new_this_month": new_this_month,
    }


def usage_stats(db: Session) -> dict:
    """Query volume, monthly and today."""
    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _sum_since(start: datetime) -> int:
        return int(db.scalar(
            select(func.coalesce(func.sum(UsageRecord.query_count), 0)).where(
                UsageRecord.created_at >= start)) or 0)

    def _users_since(start: datetime) -> int:
        return int(db.scalar(
            select(func.count(func.distinct(UsageRecord.user_id))).where(
                UsageRecord.created_at >= start)) or 0)

    return {
        "queries_this_month": _sum_since(month_start),
        "queries_today": _sum_since(today_start),
        "distinct_users_this_month": _users_since(month_start),
    }


def plan_breakdown(db: Session) -> list:
    """Subscriber counts per plan, plan order preserved."""
    plans = db.scalars(select(Plan).order_by(Plan.id)).all()
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "price_cents": plan.price_cents,
            "monthly_query_limit": plan.monthly_query_limit,
            "is_unlimited": plan.is_unlimited,
            "subscribers": int(db.scalar(
                select(func.count(User.id)).where(User.plan_id == plan.id)) or 0),
            "active_subscribers": int(db.scalar(
                select(func.count(User.id)).where(
                    User.plan_id == plan.id, User.is_active.is_(True))) or 0),
        }
        for plan in plans
    ]


def daily_usage_series(db: Session, days: int = DEFAULT_DAYS) -> list:
    """
    Per-day query counts for the last `days` days, zero-filled.

    Days with no rows are included explicitly so a chart shows a real dip rather
    than silently compressing quiet days out of the axis.
    """
    if days < 1 or days > 365:
        days = DEFAULT_DAYS
    start_date = (_now() - timedelta(days=days - 1)).date()

    rows = db.execute(
        select(
            func.date(UsageRecord.created_at).label("day"),
            func.coalesce(func.sum(UsageRecord.query_count), 0).label("queries"),
        )
        .where(UsageRecord.created_at >= start_date)
        .group_by("day")
    ).all()

    counted = {}
    for day, queries in rows:
        key = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        counted[key] = int(queries)

    series = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        series.append({"date": day.isoformat(), "queries": counted.get(day, 0)})
    return series


def upgrade_stats(db: Session) -> dict:
    """Upgrade queue depth by status."""
    rows = db.execute(
        select(UpgradeRequest.status, func.count(UpgradeRequest.id)).group_by(
            UpgradeRequest.status)
    ).all()
    counts = {status: int(total) for status, total in rows}
    # "open" is what the dashboard actually cares about: work still to do.
    counts["open"] = counts.get("pending", 0) + counts.get("contacted", 0)
    return counts


def signoff_stats(db: Session, days: int = DEFAULT_DAYS) -> dict:
    """Human sign-offs: how many, and how many recently."""
    since = _now() - timedelta(days=days)
    return {
        "total": int(db.scalar(select(func.count(ReviewSignoff.id))) or 0),
        f"last_{days}_days": int(db.scalar(
            select(func.count(ReviewSignoff.id)).where(
                ReviewSignoff.saved_at >= since)) or 0),
    }


def daily_usage_by_user(db: Session, days: int = DEFAULT_DAYS) -> dict:
    """
    Per-account query counts over the window, one gap-filled series each.

    One grouped query rather than one per account: the dashboard draws a
    sparkline for every row of the accounts table, and N+1 queries to build N
    sparklines is exactly the kind of thing that makes an admin page feel slow.

    Counts come from usage_records, not the history log, so an account's graph
    still covers runs made before the history table existed -- and so the graph,
    the quota and the billing view can never tell three different stories.

    Returns {user_id: {"series": [...], "total": int, "last_active": iso|None}}.
    Accounts with no usage in the window are simply absent; the caller renders
    an empty sparkline for them.
    """
    if days < 1 or days > 365:
        days = DEFAULT_DAYS
    start_date = (_now() - timedelta(days=days - 1)).date()

    rows = db.execute(
        select(
            UsageRecord.user_id,
            func.date(UsageRecord.created_at).label("day"),
            func.coalesce(func.sum(UsageRecord.query_count), 0).label("queries"),
        )
        .where(UsageRecord.created_at >= start_date)
        .group_by(UsageRecord.user_id, "day")
    ).all()

    counted: dict = {}
    for user_id, day, queries in rows:
        key = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        counted.setdefault(int(user_id), {})[key] = int(queries)

    axis = [start_date + timedelta(days=offset) for offset in range(days)]
    out = {}
    for user_id, per_day in counted.items():
        active_days = sorted(day for day, count in per_day.items() if count > 0)
        out[str(user_id)] = {
            "series": [
                {"date": day.isoformat(), "queries": per_day.get(day, 0)}
                for day in axis
            ],
            "total": sum(per_day.values()),
            "last_active": active_days[-1].isoformat() if active_days else None,
        }
    return out


def build_analytics(db: Session, days: int = DEFAULT_DAYS) -> dict:
    """The whole dashboard payload in one call."""
    return {
        "generated_at": _now().isoformat(),
        "window_days": days,
        "accounts": account_stats(db),
        "usage": usage_stats(db),
        "plans": plan_breakdown(db),
        "daily_usage": daily_usage_series(db, days),
        "upgrades": upgrade_stats(db),
        "signoffs": signoff_stats(db, days),
    }