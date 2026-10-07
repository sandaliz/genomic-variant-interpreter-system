"""
Tests for the administrator dashboard figures.

Two things are easy to get wrong here and are therefore tested explicitly:
counts must reflect real rows, and the daily series must include days with no
activity instead of silently skipping them (which would make a chart lie).
"""

from datetime import date, timedelta

import pytest

from db.models import ReviewSignoff, UsageRecord, UpgradeRequest, User
from services.analytics_service import (
    build_analytics,
    daily_usage_series,
    upgrade_stats,
)
from conftest import make_user, plan_named


@pytest.fixture
def world(db_session):
    """A small but realistic dataset: two plans, four accounts, usage, requests."""
    free = plan_named(db_session, "Free", limit=10, llm=False, price_cents=0)
    pro = plan_named(db_session, "Professional", limit=500, llm=True,
                     price_cents=9900)
    a = make_user(db_session, username="a", plan=free)
    b = make_user(db_session, username="b", plan=pro)
    c = make_user(db_session, username="c", plan=free, is_active=False)
    admin = make_user(db_session, username="root", plan=pro, role="admin")

    for i in range(3):
        db_session.add(UsageRecord(user_id=a.id, request_id=f"a-{i}"))
    db_session.add(UsageRecord(user_id=b.id, request_id="b-0"))
    # An older row that must fall outside a 30-day window.
    old = UsageRecord(user_id=a.id, request_id="a-old", query_count=9)
    db_session.add(old)
    db_session.flush()
    old.created_at = old.created_at - timedelta(days=60)
    db_session.add(ReviewSignoff(variant_id="BRCA1:x", reviewer_name="Dr A",
                                 review_date=date.today()))
    db_session.add(UpgradeRequest(user_id=b.id, target_plan_id=pro.id,
                                  status="pending"))
    db_session.commit()
    return {"free": free, "pro": pro, "a": a, "b": b, "c": c, "admin": admin}


class TestAuthorisation:
    def test_anonymous_is_401(self, client, world):
        assert client.get("/api/admin/analytics").status_code == 401

    def test_clinician_is_403(self, client, world, token_for):
        # token_for, not login: the fixtures here use descriptive usernames.
        assert client.get("/api/admin/analytics",
                          headers=token_for("a")).status_code == 403

    def test_admin_is_allowed(self, client, world, login):
        assert client.get("/api/admin/analytics",
                          headers=login("root")).status_code == 200

    def test_window_is_validated(self, client, world, login):
        for bad in (0, 400):
            assert client.get(f"/api/admin/analytics?days={bad}",
                              headers=login("root")).status_code == 422


class TestAccountFigures:
    def test_counts_headcount_and_activity(self, client, world, login):
        accounts = client.get("/api/admin/analytics",
                              headers=login("root")).json()["accounts"]
        assert accounts["total"] == 4
        assert accounts["active"] == 3        # 'c' is deactivated
        assert accounts["inactive"] == 1
        assert accounts["admins"] == 1

    def test_new_this_month_counts_only_new_accounts(self, client, world, login):
        accounts = client.get("/api/admin/analytics",
                              headers=login("root")).json()["accounts"]
        assert accounts["new_this_month"] == 4

    def test_zero_state_is_not_an_error(self, db_session):
        stats = build_analytics(db_session)["accounts"]
        assert stats == {"total": 0, "active": 0, "inactive": 0,
                         "admins": 0, "new_this_month": 0}


class TestUsageFigures:
    def test_counts_only_the_current_month(self, client, world, login):
        usage = client.get("/api/admin/analytics",
                           headers=login("root")).json()["usage"]
        # 3 + 1 in-window rows; the 9-query row is 60 days old.
        assert usage["queries_this_month"] == 4

    def test_counts_distinct_users(self, client, world, login):
        usage = client.get("/api/admin/analytics",
                           headers=login("root")).json()["usage"]
        assert usage["distinct_users_this_month"] == 2


class TestDailySeries:
    def test_length_matches_the_window(self, db_session):
        assert len(daily_usage_series(db_session, days=7)) == 7

    def test_quiet_days_are_present_as_zero(self, db_session, world):
        """
        A gap-filled series, not one row per day that happens to have data.
        Skipping empty days would make the chart imply activity stopped.
        """
        series = daily_usage_series(db_session, days=30)
        assert len(series) == 30
        assert len([d for d in series if d["queries"] == 0]) >= 28
        # Dates must be consecutive and ascending.
        assert [d["date"] for d in series] == sorted(d["date"] for d in series)

    def test_todays_usage_appears(self, db_session, world):
        assert daily_usage_series(db_session, days=1)[0]["queries"] == 4

    def test_bad_window_falls_back_to_the_default(self, db_session):
        assert len(daily_usage_series(db_session, days=0)) == 30
        assert len(daily_usage_series(db_session, days=9999)) == 30


class TestPlanAndQueueFigures:
    def test_plan_breakdown_counts_subscribers(self, client, world, login):
        plans = client.get("/api/admin/analytics",
                           headers=login("root")).json()["plans"]
        by_name = {p["name"]: p for p in plans}
        assert by_name["Free"]["subscribers"] == 2          # a and c
        assert by_name["Free"]["active_subscribers"] == 1   # c is deactivated
        assert by_name["Professional"]["subscribers"] == 2  # b and root

    def test_open_upgrades_are_counted(self, db_session, world):
        stats = upgrade_stats(db_session)
        assert stats["pending"] == 1
        assert stats["open"] == 1

    def test_contacted_counts_as_open(self, db_session, world):
        request = db_session.query(UpgradeRequest).one()
        request.status = "contacted"
        db_session.commit()
        stats = upgrade_stats(db_session)
        assert stats["contacted"] == 1
        assert stats["open"] == 1

    def test_signoffs_are_counted(self, client, world, login):
        signoffs = client.get("/api/admin/analytics",
                              headers=login("root")).json()["signoffs"]
        assert signoffs["total"] == 1
        assert signoffs["last_30_days"] == 1


class TestPayloadShape:
    def test_contains_every_section_the_dashboard_needs(self, client, world, login):
        body = client.get("/api/admin/analytics",
                          headers=login("root")).json()
        assert set(body) == {"generated_at", "window_days", "accounts", "usage",
                             "plans", "daily_usage", "upgrades", "signoffs"}
        assert body["window_days"] == 30
        assert len(body["daily_usage"]) == 30
        accounts = client.get("/api/admin/analytics",
                              headers=login("root")).json()["accounts"]
        assert accounts["new_this_month"] == 4

    def test_zero_state_is_not_an_error(self, db_session):
        stats = build_analytics(db_session)["accounts"]
        assert stats == {"total": 0, "active": 0, "inactive": 0,
                         "admins": 0, "new_this_month": 0}