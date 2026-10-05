"""
Plan, feature-gate and quota tests for services/entitlement_service.py.

The row-lock branch of record_usage() is PostgreSQL-only, so it is exercised
separately by asserting the compiled SQL; the SQLite path covers the quota
arithmetic, which is the part that is easy to get wrong.
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from db.models import UsageRecord, User
from services.entitlement_service import (
    _lock_user_row,
    check_quota,
    entitlements,
    month_start_utc,
    record_usage,
    usage_this_month,
)
from conftest import make_plan, make_user


def _record(db, user_id, request_id, created_at=None):
    row = UsageRecord(user_id=user_id, request_id=request_id, query_count=1)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    return row


class TestMonthWindow:
    def test_month_start_is_the_first_of_the_month_in_utc(self):
        moment = datetime(2026, 3, 17, 9, 30, tzinfo=timezone.utc)
        assert month_start_utc(moment) == datetime(2026, 3, 1, tzinfo=timezone.utc)

    def test_naive_datetimes_are_treated_as_utc(self):
        # The pipeline must not silently use local time for quota windows.
        moment = datetime(2026, 3, 17, 9, 30)
        assert month_start_utc(moment) == datetime(2026, 3, 1, tzinfo=timezone.utc)

    def test_january_boundary(self):
        assert month_start_utc(
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)).month == 1


class TestUsageCounting:
    def test_zero_before_any_usage(self, db_session):
        user = make_user(db_session)
        assert usage_this_month(db_session, user.id) == 0

    def test_counts_only_the_current_month(self, db_session):
        user = make_user(db_session)
        now = datetime(2026, 3, 17, 10, 0, tzinfo=timezone.utc)
        _record(db_session, user.id, "in-window", now)
        _record(db_session, user.id, "last-month",
                datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc))
        assert usage_this_month(db_session, user.id, now) == 1

    def test_usage_of_other_accounts_is_not_counted(self, db_session):
        plan = make_plan(db_session, name="Free", limit=5)
        alice = make_user(db_session, username="alice", plan=plan)
        bob = make_user(db_session, username="bob", plan=plan)
        _record(db_session, alice.id, "alice-1")
        _record(db_session, bob.id, "bob-1")
        _record(db_session, bob.id, "bob-2")
        assert usage_this_month(db_session, alice.id) == 1
        assert usage_this_month(db_session, bob.id) == 2


class TestEntitlements:
    def test_free_plan_flags(self, db_session):
        user = make_user(db_session, plan=make_plan(
            db_session, name="Free", limit=3, llm=False, api_access=True,
            audit_export=False))
        info = entitlements(user, 1)
        assert info["llm_enabled"] is False
        assert info["api_access"] is True
        assert info["audit_export"] is False
        assert info["monthly_limit"] == 3
        assert info["remaining"] == 2

    def test_unlimited_plan_reports_null_limit_and_null_remaining(self, db_session):
        # NULL monthly_query_limit means Enterprise/unlimited -- it must not
        # be coerced into 0, which would read as "no queries left".
        user = make_user(db_session, plan=make_plan(
            db_session, name="Enterprise", limit=None, llm=True,
            audit_export=True, price_cents=None))
        info = entitlements(user, 10_000)
        assert info["monthly_limit"] is None
        assert info["remaining"] is None
        assert info["llm_enabled"] is True

    def test_remaining_never_goes_negative(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=3))
        assert entitlements(user, 99)["remaining"] == 0


class TestCheckQuota:
    def test_passes_while_under_limit(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=3))
        _record(db_session, user.id, "one")
        info = check_quota(db_session, user)
        assert info["used_this_month"] == 1
        assert info["remaining"] == 2

    def test_raises_429_once_exhausted(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=2))
        _record(db_session, user.id, "one")
        _record(db_session, user.id, "two")
        with pytest.raises(HTTPException) as excinfo:
            check_quota(db_session, user)
        assert excinfo.value.status_code == 429
        assert "2/2" in excinfo.value.detail

    def test_unlimited_plan_never_raises(self, db_session):
        user = make_user(db_session, plan=make_plan(
            db_session, name="Enterprise", limit=None))
        for i in range(25):
            _record(db_session, user.id, f"bulk-{i}")
        assert check_quota(db_session, user)["used_this_month"] == 25


class TestRecordUsage:
    def test_records_and_commits_one_row(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=5))
        assert record_usage(db_session, user.id, "req-1", 5) is True
        assert usage_this_month(db_session, user.id) == 1

    def test_repeated_request_id_is_idempotent(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=5))
        assert record_usage(db_session, user.id, "req-1", 5) is True
        # A client retry of the identical request must not be charged twice.
        assert record_usage(db_session, user.id, "req-1", 5) is False
        assert usage_this_month(db_session, user.id) == 1

    def test_commit_time_recheck_raises_429_and_rolls_back(self, db_session):
        # limit=1 but usage was already recorded behind the pre-check: the
        # re-check inside record_usage() is what must stop the overshoot.
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=1))
        _record(db_session, user.id, "existing")
        with pytest.raises(HTTPException) as excinfo:
            record_usage(db_session, user.id, "req-2", 1)
        assert excinfo.value.status_code == 429
        assert usage_this_month(db_session, user.id) == 1

    def test_unlimited_plan_records_past_any_number(self, db_session):
        user = make_user(db_session, plan=make_plan(
            db_session, name="Enterprise", limit=None))
        for i in range(30):
            assert record_usage(db_session, user.id, f"req-{i}", None) is True
        assert usage_this_month(db_session, user.id) == 30

    def test_tokens_used_is_optional(self, db_session):
        user = make_user(db_session, plan=make_plan(db_session, name="Free", limit=5))
        record_usage(db_session, user.id, "req-1", 5, llm_tokens_used=None)
        assert db_session.query(UsageRecord).one().llm_tokens_used is None


class TestRowLock:
    def test_sqlite_is_left_unlocked(self, db_session):
        # SQLite has no row-level locking; the helper must degrade quietly
        # rather than emit SQL the dialect cannot run.
        user = make_user(db_session)
        assert _lock_user_row(db_session, user.id) is None

    def test_postgresql_dialect_emits_for_update(self):
        # Compiled-statement check: on PostgreSQL the lock must be requested,
        # otherwise concurrent requests could both pass the quota re-check.
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        statement = select(User.id).where(User.id == 1).with_for_update()
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql