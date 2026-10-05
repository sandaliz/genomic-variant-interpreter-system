"""
services/history_service.py

The per-account query log: what was asked, and what came back.

Two audiences, one table. A clinician uses it to reopen a variant they ran last
week without re-typing the HGVS expression; an administrator uses it to see the
actual queries behind an account's usage count instead of only that count.

Design notes
------------
* Recording is **best-effort**. It runs after the pipeline has already produced
  a report, so a failure here must never turn a successful classification into a
  failed request -- see record_history().
* The row stores the summary the list needs to render, not the whole response.
  A full report is reconstructible by re-running the query, and a stored copy
  would silently drift from the evidence it was computed from.
* request_id is the caller's, and unique, so a client retry cannot record twice.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.models import QueryHistory, User

# How many rows an account may browse at once, and what a caller gets by default.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Rows retained per account. The log is a convenience, not a system of record --
# the audit export is what an auditor reads -- so it is trimmed oldest-first
# rather than allowed to grow without bound.
RETENTION_PER_USER = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clamp_limit(limit: Optional[int]) -> int:
    """Keep a caller-supplied limit inside what the endpoint will serve."""
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def record_history(
    db: Session,
    user_id: int,
    request_id: str,
    query_text: str,
    result: dict,
) -> Optional[QueryHistory]:
    """
    Store one accepted run. Returns the row, or None if it could not be stored.

    Never raises on a database failure. The caller has already produced a valid
    report by this point, and losing a history row is a far smaller harm than
    turning a good answer into a 500 -- which is also what would happen if this
    ran against a database that has not had the migration applied yet. The
    failed statement still aborts the surrounding transaction, so the session is
    rolled back before returning.
    """
    entity = result.get("extracted_entities") or {}
    row = QueryHistory(
        user_id=user_id,
        request_id=request_id,
        query_text=(query_text or "")[:1000],
        variant_id=(result.get("variant_id") or None),
        trace_id=(result.get("trace_id") or None),
        gene=(entity.get("gene") or None),
        classification=(result.get("classification") or None),
        confidence=(result.get("confidence") or None),
        confidence_numeric=result.get("confidence_numeric"),
        reasoning_mode=result.get("reasoning_mode"),
        created_at=_now(),
    )
    try:
        db.add(row)
        db.flush()
        _trim(db, user_id)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return None
    return row


def _trim(db: Session, user_id: int) -> None:
    """Drop the oldest rows past the retention window for this account."""
    keep = db.scalars(
        select(QueryHistory.id)
        .where(QueryHistory.user_id == user_id)
        .order_by(QueryHistory.created_at.desc(), QueryHistory.id.desc())
        .limit(RETENTION_PER_USER)
    ).all()
    if len(keep) < RETENTION_PER_USER:
        return
    db.execute(
        delete(QueryHistory).where(
            QueryHistory.user_id == user_id,
            QueryHistory.id.notin_(list(keep)),
        )
    )


def _serialize(row: QueryHistory) -> dict:
    return {
        "id": row.id,
        "query_text": row.query_text,
        "variant_id": row.variant_id,
        "gene": row.gene,
        "classification": row.classification,
        "confidence": row.confidence,
        "confidence_numeric": row.confidence_numeric,
        "reasoning_mode": row.reasoning_mode,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def history_for_user(db: Session, user_id: int, limit: Optional[int] = None) -> list:
    """This account's recent runs, newest first."""
    rows = db.scalars(
        select(QueryHistory)
        .where(QueryHistory.user_id == user_id)
        .order_by(QueryHistory.created_at.desc(), QueryHistory.id.desc())
        .limit(clamp_limit(limit))
    ).all()
    return [_serialize(row) for row in rows]


def history_for_account(
    db: Session, user: User, limit: Optional[int] = None
) -> list:
    """One account's runs, for the administrator's per-account view."""
    return history_for_user(db, user.id, limit)


def history_count(db: Session, user_id: int) -> int:
    """How many runs this account has on file, for a truncated-list note."""
    return int(db.scalar(
        select(func.count(QueryHistory.id)).where(QueryHistory.user_id == user_id)
    ) or 0)


def daily_history_series(db: Session, user_id: int, days: int = 30) -> list:
    """
    This account's per-day query counts, oldest first, zero-filled.

    Same gap-filling rule as the dashboard's series: a quiet Tuesday is a real
    zero and has to appear on the axis, or the chart implies activity that did
    not happen.
    """
    if days < 1 or days > 365:
        days = 30
    start = (_now() - timedelta(days=days - 1)).date()
    rows = db.scalars(
        select(QueryHistory).where(
            QueryHistory.user_id == user_id,
            QueryHistory.created_at >= start,
        )
    ).all()

    counted: dict = {}
    for row in rows:
        created = row.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        key = created.astimezone(timezone.utc).date()
        counted[key] = counted.get(key, 0) + 1

    series = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        series.append({"date": day.isoformat(), "queries": counted.get(day, 0)})
    return series
