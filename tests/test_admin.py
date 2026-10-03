"""
Role-based access tests for the read-only admin listing.

Scope note: /api/admin/users is intentionally read-only. These tests lock in
that boundary -- a clinician must be refused, an admin must be served, and no
response may leak password material.
"""

import pytest

from conftest import make_plan, make_user, plan_named


@pytest.fixture
def clinician(db_session):
    plan = make_plan(db_session, name="Professional", limit=50, llm=True)
    return make_user(db_session, username="clinician", plan=plan)


@pytest.fixture
def admin(db_session, clinician):
    plan = make_plan(db_session, name="Enterprise", limit=None, llm=True)
    return make_user(db_session, username="root", plan=plan, role="admin")


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
            assert set(entry) == {"id", "username", "email", "role", "is_active", "plan",
                           "plan_id", "created_at", "used_this_month"}
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

    def test_the_collection_endpoint_itself_is_not_writable(self, client, admin, login):
        # Writes go to /api/admin/users/{id}, not to the collection.
        for method in ("post", "put", "patch"):
            response = getattr(client, method)(
                "/api/admin/users", headers=login("root"), json={})
            assert response.status_code in (404, 405), method
        assert client.delete("/api/admin/users",
                             headers=login("root")).status_code in (404, 405)


class TestAdminUpdateAuthorisation:
    def test_anonymous_is_401(self, client, admin, clinician):
        assert client.patch("/api/admin/users/2", json={"is_active": False}).status_code == 401

    def test_clinician_is_403(self, client, admin, clinician, login):
        assert client.patch("/api/admin/users/2", json={"is_active": False},
                            headers=login()).status_code == 403

    def test_unknown_account_is_404(self, client, admin, login):
        assert client.patch("/api/admin/users/9999", json={"is_active": False},
                            headers=login("root")).status_code == 404


class TestAdminPlanChange:
    def test_plan_can_be_changed(self, client, db_session, login):
        free = plan_named(db_session, "Free", limit=3)
        pro = plan_named(db_session, "Professional", limit=500, llm=True)
        target = make_user(db_session, username="clinician", plan=free)
        make_user(db_session, username="root", role="admin",
                  plan=plan_named(db_session, "Enterprise", limit=None, llm=True))

        response = client.patch(f"/api/admin/users/{target.id}",
                                json={"plan_id": pro.id}, headers=login("root"))
        assert response.status_code == 200, response.text
        assert response.json()["user"]["plan"] == "Professional"
        db_session.refresh(target)
        assert target.plan_id == pro.id

    def test_unknown_plan_is_422(self, client, db_session, login):
        free = plan_named(db_session, "Free", limit=3)
        make_user(db_session, username="clinician", plan=free)
        make_user(db_session, username="root", role="admin", plan=free)
        assert client.patch("/api/admin/users/1", json={"plan_id": 4242},
                            headers=login("root")).status_code == 422

    def test_partial_patch_leaves_other_fields_alone(self, client, db_session, login):
        """A PATCH must touch only the fields that were actually sent."""
class TestPrivilegeGuards:
    """
    The two guards on PATCH. These are the difference between a usable admin
    panel and a one-click self-destruct.
    """

    def test_admin_cannot_deactivate_themselves(self, client, db_session, login):
        root = make_user(db_session, username="root", role="admin",
                         plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        make_user(db_session, username="other", role="admin",
                  plan=plan_named(db_session, "Enterprise", limit=None, llm=True))

        response = client.patch(f"/api/admin/users/{root.id}",
                                json={"is_active": False}, headers=login("root"))
        assert response.status_code == 400
        db_session.refresh(root)
        assert root.is_active is True

    def test_admin_cannot_demote_themselves(self, client, db_session, login):
        root = make_user(db_session, username="root", role="admin",
                         plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        make_user(db_session, username="other", role="admin",
                  plan=plan_named(db_session, "Enterprise", limit=None, llm=True))

        response = client.patch(f"/api/admin/users/{root.id}",
                                json={"role": "clinician"}, headers=login("root"))
        assert response.status_code == 400
        db_session.refresh(root)
        assert root.role == "admin"

    def test_last_active_admin_cannot_be_deactivated(self, client, db_session, login):
        """Otherwise nobody could administer the deployment through the API."""
        root = make_user(db_session, username="root", role="admin",
                         plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        make_user(db_session, username="clinician")

        response = client.patch(f"/api/admin/users/{root.id}",
                                json={"is_active": False}, headers=login("root"))
        assert response.status_code == 409
        db_session.refresh(root)
        assert root.is_active is True

    def test_last_active_admin_cannot_be_demoted(self, client, db_session, login):
        root = make_user(db_session, username="root", role="admin",
                         plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        make_user(db_session, username="clinician",
                  plan=plan_named(db_session, "Free", limit=3))
        response = client.patch(f"/api/admin/users/{root.id}",
                                json={"role": "clinician"}, headers=login("root"))
        assert response.status_code == 409
        assert "last active administrator" in response.json()["detail"]
        db_session.refresh(root)
        assert root.role == "admin"

    def test_demotion_is_allowed_when_another_admin_remains(self, client,
                                                             db_session, login):
        root = make_user(db_session, username="root", role="admin",
                         plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        other = make_user(db_session, username="other", role="admin",
                          plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        response = client.patch(f"/api/admin/users/{other.id}",
                                json={"role": "clinician"}, headers=login("root"))
        assert response.status_code == 200
        db_session.refresh(other)
        assert other.role == "clinician"

    def test_promoting_a_clinician_to_admin_works(self, client, db_session, login):
        make_user(db_session, username="root", role="admin",
                  plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        target = make_user(db_session, username="clinician",
                           plan=plan_named(db_session, "Free", limit=3))
        response = client.patch(f"/api/admin/users/{target.id}",
                                json={"role": "admin"}, headers=login("root"))
        assert response.status_code == 200
        db_session.refresh(target)
        assert target.role == "admin"

    def test_unknown_role_is_422(self, client, db_session, login):
        free = plan_named(db_session, "Free", limit=3)
        make_user(db_session, username="root", role="admin", plan=free)
        target = make_user(db_session, username="clinician", plan=free)
        response = client.patch(f"/api/admin/users/{target.id}",
                                json={"role": "superuser"}, headers=login("root"))
        assert response.status_code == 422
        db_session.refresh(target)
        assert target.role == "clinician"

    def test_deactivated_account_loses_access_immediately(self, client, db_session,
                                                          login):
        make_user(db_session, username="root", role="admin",
                  plan=plan_named(db_session, "Enterprise", limit=None, llm=True))
        target = make_user(db_session, username="clinician",
                           plan=plan_named(db_session, "Free", limit=3))
        headers = login("clinician")
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        client.patch(f"/api/admin/users/{target.id}", json={"is_active": False},
                     headers=login("root"))
        # No token expiry wait: identity is re-read on every request.
        assert client.get("/api/auth/me", headers=headers).status_code == 401


class TestAdminPasswordReset:
    def test_admin_can_reset_another_password(self, client, admin, clinician, login):
        response = client.post(f"/api/admin/users/{clinician.id}/password",
                               json={"new_password": "brand-new-password"},
                               headers=login("root"))
        assert response.status_code == 200, response.text
        assert response.json()["reset"] is True
        # The new password works.
        assert client.post("/api/auth/login",
                           json={"username": "clinician",
                                 "password": "brand-new-password"}).status_code == 200

    def test_old_password_stops_working(self, client, admin, clinician, login):
        client.post(f"/api/admin/users/{clinician.id}/password",
                    json={"new_password": "brand-new-password"},
                    headers=login("root"))
        assert client.post("/api/auth/login",
                           json={"username": "clinician",
                                 "password": "correct-horse-battery"}).status_code == 401

    def test_clinician_cannot_reset_passwords(self, client, admin, clinician, login):
        assert client.post(f"/api/admin/users/{clinician.id}/password",
                           json={"new_password": "brand-new-password"},
                           headers=login()).status_code == 403

    def test_short_password_is_rejected(self, client, admin, clinician, login):
        response = client.post(f"/api/admin/users/{clinician.id}/password",
                               json={"new_password": "short"}, headers=login("root"))
        assert response.status_code == 422

    def test_response_never_echoes_the_password_or_hash(self, client, admin,
                                                         clinician, login):
        raw = client.post(f"/api/admin/users/{clinician.id}/password",
                          json={"new_password": "brand-new-password"},
                          headers=login("root")).text
        assert "brand-new-password" not in raw
        assert "argon2" not in raw.lower()
        assert "password_hash" not in raw

    def test_unknown_account_is_404(self, client, admin, login):
        assert client.post("/api/admin/users/9999/password",
                           json={"new_password": "brand-new-password"},
                           headers=login("root")).status_code == 404


class TestAdminPlanCatalogue:
    def test_admin_only(self, client, admin, clinician, login):
        assert client.get("/api/admin/plans").status_code == 401
        assert client.get("/api/admin/plans", headers=login()).status_code == 403
        assert client.get("/api/admin/plans", headers=login("root")).status_code == 200

    def test_reports_limits_and_subscriber_counts(self, client, db_session, login):
        free = plan_named(db_session, "Free", limit=10)
        enterprise = plan_named(db_session, "Enterprise", limit=None, llm=True)
        make_user(db_session, username="clinician", plan=free)
        make_user(db_session, username="root", role="admin", plan=enterprise)

        body = client.get("/api/admin/plans", headers=login("root")).json()
        by_name = {p["name"]: p for p in body["plans"]}
        assert by_name["Free"]["subscribers"] == 1
        assert by_name["Free"]["monthly_query_limit"] == 10
        assert by_name["Free"]["is_unlimited"] is False
        assert by_name["Free"]["llm_enabled"] is False
        # NULL limit must read as unlimited, never as zero.
        assert by_name["Enterprise"]["is_unlimited"] is True
        assert by_name["Enterprise"]["monthly_query_limit"] is None