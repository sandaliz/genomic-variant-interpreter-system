"""
services/entitlement_service.py

Plan enforcement for VariantLens: quota checks, feature gates, usage records.

Monthly windows are computed from usage_records timestamps in UTC and never from
a mutable counter, so history is preserved and months cannot bleed into each other.

Quota enforcement is two-layered:
  1. check_quota() runs before the (expensive) agent pipeline and rejects
     over-quota users immediately.
  2. record_usage() locks the account row, then re-verifies inside the same
     transaction that writes the usage row, so two concurrent requests for the
     same account cannot both slip through the pre-check and exceed the limit.

The row lock (SELECT ... FOR UPDATE) is the piece that makes step 2 correct:
without it two sessions can each observe used < limit and both insert. SQLite
has no row-level locking, so it is skipped there and the fallback is the
existing insert-then-recheck, which is what the fast unit tests exercise.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import UsageRecord, User

# Dialects that implement SELECT ... FOR UPDATE. PostgreSQL is the production
# target; MySQL/MariaDB are listed because they behave the same way.
_ROW_LOCK_DIALECTS = {"postgresql", "mysql", "mariadb"}


def month_start_utc(now: Optional[datetime] = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_this_month(db: Session, user_id: int, now: Optional[datetime] = None) -> int:
    start = month_start_utc(now)
    total = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.query_count), 0)).where(
            UsageRecord.user_id == user_id,
            UsageRecord.created_at >= start,
        )
    )
    return int(total or 0)


def entitlements(user: User, used: int) -> dict:
    limit = user.plan.monthly_query_limit
    remaining = None if limit is None else max(limit - used, 0)
    return {
        "llm_enabled": bool(user.plan.llm_enabled),
        "api_access": bool(user.plan.api_access),
        "audit_export": bool(user.plan.audit_export),
        "monthly_limit": limit,
        "used_this_month": used,
        "remaining": remaining,
    }


def _exhausted_detail(used: int, limit: int) -> str:
    return (
        f"Monthly quota exhausted: {used}/{limit} queries used. "
        "Upgrade the plan or wait for next month."
    )


def check_quota(db: Session, user: User, now: Optional[datetime] = None) -> dict:
    """Fast pre-check before the agent pipeline. Raises 429 when exhausted."""
    limit = user.plan.monthly_query_limit
    used = usage_this_month(db, user.id, now)
    if limit is not None and used >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_exhausted_detail(used, limit),
        )
    return entitlements(user, used)


def _lock_user_row(db: Session, user_id: int) -> None:
    """
    Take a row-level write lock on the account for the rest of the transaction.

    This is what makes the re-check in record_usage() correct under concurrency:
    without it two requests for the same account can each read used < limit and
    both insert a usage row. No-op on SQLite, which has no row-level locks (its
    single-writer model serialises transactions anyway, and the fast unit tests
    exercise that fallback path).
    """
    bind = db.get_bind()
    if bind.dialect.name in _ROW_LOCK_DIALECTS:
        db.execute(select(User.id).where(User.id == user_id).with_for_update())
def record_usage(
    db: Session,
    user_id: int,
    request_id: str,
    limit: Optional[int],
    llm_tokens_used: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    """
    Lock the account, insert one usage row, and re-verify the quota inside a
    single transaction.

    Returns False when this request_id was already recorded (client retry --
    the quota is not charged twice). Raises 429 when the insert would push
    the account over its monthly limit. Commits on success, so callers must
    not commit again afterwards on this session.
    """
    _lock_user_row(db, user_id)
    db.add(UsageRecord(
        user_id=user_id,
        request_id=request_id,
        query_count=1,
        llm_tokens_used=llm_tokens_used,
    ))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    month_start = month_start_utc(now)
    used = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.query_count), 0)).where(
            UsageRecord.user_id == user_id,
            UsageRecord.created_at >= month_start,
        )
    )
    used = int(used or 0)
    if limit is not None and used > limit:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_exhausted_detail(used, limit),
        )
    db.commit()
    return True
