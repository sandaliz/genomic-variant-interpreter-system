"""
Role-based access tests for the read-only admin listing.

Scope note: /api/admin/users is intentionally read-only. These tests lock in
that boundary -- a clinician must be refused, an admin must be served, and no
response may leak password material.
"""

import pytest

from conftest import make_plan, make_user


@pytest.fixture
def clinician(db_session):
    plan = make_plan(db_session, name="Professional", limit=50, llm=True)
    make_user(db_session, username="clinician", plan=plan)
    return "clinician"


@pytest.fixture
def admin(db_session, clinician):
    plan = make_plan(db_session, name="Enterprise", limit=None, llm=True)
    make_user(db_session, username="root", plan=plan, role="admin")
    return "root"


class TestAdminListingAuthorisation:
    def test_anonymous_request_is_401(self, client, admin):
        assert client.get("/api/admin/users").status_code == 401

    def test_clinician_is_403(self, client, clinician, login):
        response = client.get("/api/admin/users", headers=login())
        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()

    def test_admin_is_allowed(self, client, admin, login):
        response = client.get("/api/admin/users", headers=login("root"))
        assert response.status_code == 200, response.text

    def test_deactivated_admin_loses_access_immediately(self, client, admin, login,
                                                        db_session):
        headers = login("root")
        assert client.get("/api/admin/users", headers=headers).status_code == 200

        from db.models import User
        db_session.query(User).filter_by(username="root").one().is_active = False
        db_session.commit()
        assert client.get("/api/admin/users", headers=headers).status_code == 401

    def test_admin_on_the_free_plan_is_still_an_admin(self, db_session):
        # Role and plan are independent axes: a subscription tier must never
        # decide who is an administrator.
        plan = make_plan(db_session, name="Free", limit=1)
        make_user(db_session, username="root", plan=plan, role="admin")
        assert plan.name == "Free"


class TestAdminListingContent:
    def test_lists_accounts_with_plan_and_usage(self, client, admin, clinician, login):
        body = client.get("/api/admin/users", headers=login("root")).json()
        usernames = {u["username"] for u in body["users"]}
        assert {"root", "clinician"} <= usernames
        for entry in body["users"]:
            assert set(entry) == {"id", "username", "email", "role", "is_active",
                                  "plan", "created_at", "used_this_month"}
            assert entry["used_this_month"] == 0

    def test_no_password_material_is_ever_returned(self, client, admin, login):
        raw = client.get("/api/admin/users", headers=login("root")).text
        assert "password" not in raw.lower()
        assert "$argon2" not in raw

    def test_limit_is_validated(self, client, admin, login):
        for bad in (0, -1, 501):
            response = client.get(f"/api/admin/users?limit={bad}",
                                  headers=login("root"))
            assert response.status_code == 422, bad

    def test_limit_caps_the_result_set(self, client, db_session, login):
        plan = make_plan(db_session, name="Free", limit=5)
        for i in range(4):
            make_user(db_session, username=f"u{i}", plan=plan)
        make_user(db_session, username="root", plan=plan, role="admin")
        body = client.get("/api/admin/users?limit=2", headers=login("root")).json()
        assert body["total"] == 2

    def test_the_endpoint_is_read_only(self, client, admin, login):
        # No mutating verbs are exposed in this scope.
        for method in ("post", "put", "patch"):
            response = getattr(client, method)(
                "/api/admin/users", headers=login("root"), json={})
            assert response.status_code in (404, 405), method
        assert client.delete("/api/admin/users",
                             headers=login("root")).status_code in (404, 405)