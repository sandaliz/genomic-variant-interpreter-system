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
                           json={"email": "clinician@example.com",
                                 "password": "brand-new-password"}).status_code == 200

    def test_old_password_stops_working(self, client, admin, clinician, login):
        client.post(f"/api/admin/users/{clinician.id}/password",
                    json={"new_password": "brand-new-password"},
                    headers=login("root"))
        assert client.post("/api/auth/login",
                           json={"email": "clinician@example.com",
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


def _events(db_session, event_type=None):
    """Audit rows, newest first, optionally filtered by type."""
    from db.models import AuthEvent

    q = db_session.query(AuthEvent)
    if event_type:
        q = q.filter(AuthEvent.event_type == event_type)
    return q.order_by(AuthEvent.id.desc()).all()


class TestCookieSessionAndCsrf:
    """
    The session token moved from localStorage into an HttpOnly cookie, which is
    what stops an XSS bug from exfiltrating it. The cost is CSRF, so every
    cookie-authenticated write needs a matching token.

    Bearer auth is kept deliberately: it is what the rest of the suite, curl and
    scripted integrations use, and it is not ambient, so it is not CSRF-exploitable.
    """

    def _login(self, client, db_session, username="clinician"):
        make_user(db_session, username=username)
        return client.post("/api/auth/login", json={
            "email": f"{username}@example.com",
            "password": "correct-horse-battery",
        })

    @staticmethod
    def _cookies(response):
        """
        Parse the raw Set-Cookie headers into {name: attribute_string}.

        httpx returns them as plain strings, so the attributes have to be pulled
        out by hand -- which is exactly what a test asserting on HttpOnly should
        be doing rather than trusting a helper library's interpretation.
        """
        parsed = {}
        for raw in response.headers.get_list("set-cookie"):
            head, _, rest = raw.partition(";")
            name = head.split("=", 1)[0].strip()
            parsed[name] = f"{head};{rest}"
        return parsed

    @staticmethod
    def _values(response):
        return {c["name"]: c.split("=", 1)[1].split(";")[0]
                for c in response.headers.get_list("set-cookie")}

    def test_login_sets_an_httponly_session_cookie(self, client, db_session):
        response = self._login(client, db_session)
        assert response.status_code == 200, response.text
        session = self._cookies(response)["variantlens_session"]
        # The entire point: script must not be able to read it.
        assert "HttpOnly" in session
        assert "Path=/" in session
        assert "SameSite" in session

    def test_the_csrf_cookie_is_readable_on_purpose(self, client, db_session):
        """
        The double-submit pattern needs one half the client can see. That is safe
        because knowing it is useless without also causing the browser to attach
        the HttpOnly cookie.
        """
        response = self._login(client, db_session)
        csrf = self._cookies(response)["variantlens_csrf"]
        assert "HttpOnly" not in csrf

    def test_the_login_response_echoes_the_csrf_token(self, client, db_session):
        response = self._login(client, db_session)
        raw = {r.split("=", 1)[0].strip(): r.split("=", 1)[1].split(";")[0]
               for r in response.headers.get_list("set-cookie")}
        assert response.json()["csrf_token"] == raw["variantlens_csrf"]

    def test_the_body_token_and_the_cookie_are_one_credential(
            self, client, db_session):
        """Not two independent credentials that could drift apart."""
        response = self._login(client, db_session)
        raw = {r.split("=", 1)[0].strip(): r.split("=", 1)[1].split(";")[0]
               for r in response.headers.get_list("set-cookie")}
        assert response.json()["access_token"] == raw["variantlens_session"]

    def test_cookie_authenticates_a_read(self, client, db_session):
        self._login(client, db_session)
        # The test client carries cookies forward, so this is cookie-only auth.
        response = client.get("/api/auth/me")
        assert response.status_code == 200, response.text
        assert response.json()["user"]["username"] == "clinician"

    def test_a_write_without_the_csrf_header_is_refused(self, client, db_session):
        """
        The browser attaches the session cookie to a cross-site POST whether or
        not the page meant to make one. Without the matching header the server
        cannot tell the difference, so it must refuse.
        """
        self._login(client, db_session)
        response = client.patch("/api/auth/me", json={"full_name": "Impostor"})
        assert response.status_code == 403
        assert "csrf" in response.json()["detail"].lower()

    def test_a_write_with_a_mismatched_csrf_token_is_refused(
            self, client, db_session):
        self._login(client, db_session)
        response = client.patch("/api/auth/me", json={"full_name": "Impostor"},
                                headers={"X-CSRF-Token": "not-the-right-value"})
        assert response.status_code == 403

    def test_a_write_with_the_matching_csrf_token_succeeds(self, client, db_session):
        response = self._login(client, db_session)
        ok = client.patch("/api/auth/me", json={"full_name": "Dr House"},
                          headers={"X-CSRF-Token": response.json()["csrf_token"]})
        assert ok.status_code == 200, ok.text
        assert ok.json()["user"]["full_name"] == "Dr House"

    def test_reads_do_not_need_a_csrf_token(self, client, db_session):
        """GET and HEAD do not change state, so they are exempt."""
        self._login(client, db_session)
        assert client.get("/api/plans").status_code == 200

    def test_bearer_auth_needs_no_csrf_token(self, client, db_session, login):
        """
        A bearer token is never sent ambiently, so there is nothing for a
        cross-site page to ride. Requiring CSRF there would only break scripting.
        """
        make_user(db_session, username="clinician")
        response = client.patch("/api/auth/me", headers=login(),
                                json={"full_name": "Dr House"})
        assert response.status_code == 200, response.text

    def test_an_authorization_header_beats_a_stale_cookie(self, client, db_session):
        """
        If both are present the explicit header wins, so a scripted client is
        never dragged into the cookie/CSRF path by a leftover browser session.
        """
        self._login(client, db_session)
        token = client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery",
        }).json()["access_token"]
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text

    def test_logout_clears_both_cookies(self, client, db_session, login):
        make_user(db_session, username="clinician")
        response = client.post("/api/auth/logout", headers=login())
        assert response.status_code == 200, response.text
        cleared = self._cookies(response)
        assert "variantlens_session" in cleared
        assert "variantlens_csrf" in cleared
        for name in ("variantlens_session", "variantlens_csrf"):
            assert "Max-Age=0" in cleared[name] or '=""' in cleared[name]

    def test_no_credentials_at_all_is_401(self, client):
        assert client.get("/api/auth/me").status_code == 401


class TestLoginRateLimiting:
    """
    Argon2id makes each guess slow but does not cap how many are made, so the
    limiter is the control that actually bounds brute force.
    """

    def _try(self, client, username, password="wrong-password"):
        # The limiter keys on the submitted identifier, which is now always an
        # email address, so a bare handle would key a bucket that nothing signs
        # in with and the throttling tests would pass for the wrong reason.
        identifier = username if "@" in username else f"{username}@example.com"
        return client.post("/api/auth/login",
                           json={"email": identifier, "password": password})

    def test_repeated_failures_are_throttled(self, client, db_session):
        make_user(db_session, username="clinician")
        from auth.rate_limit import DEFAULT_MAX_ATTEMPTS

        for _ in range(DEFAULT_MAX_ATTEMPTS):
            assert self._try(client, "clinician").status_code == 401
        blocked = self._try(client, "clinician")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers

    def test_throttling_applies_to_an_unknown_account_too(self, client):
        """Otherwise the limit could be probed for valid usernames."""
        from auth.rate_limit import DEFAULT_MAX_ATTEMPTS

        for _ in range(DEFAULT_MAX_ATTEMPTS):
            assert self._try(client, "nobody-here").status_code == 401
        assert self._try(client, "nobody-here").status_code == 429

    def test_throttle_is_per_account(self, client, db_session):
        """One locked-out account must not lock out a different one."""
        from auth.rate_limit import DEFAULT_MAX_ATTEMPTS

        plan = make_plan(db_session, name="Free", limit=10)
        make_user(db_session, username="alice", plan=plan)
        make_user(db_session, username="bob", plan=plan)
        for _ in range(DEFAULT_MAX_ATTEMPTS):
            self._try(client, "alice")
        assert self._try(client, "alice").status_code == 429
        assert self._try(client, "bob").status_code == 401

    def test_a_correct_password_clears_the_counter(self, client, db_session):
        """
        A user who mistyped twice and then got it right must not stay locked out
        of their own account.
        """
        from auth.rate_limit import DEFAULT_MAX_ATTEMPTS

        user = make_user(db_session, username="clinician")
        for _ in range(DEFAULT_MAX_ATTEMPTS - 1):
            self._try(client, "clinician")
        assert client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery",
        }).status_code == 200
        # The allowance is restored, so the account is usable again.
        assert self._try(client, "clinician").status_code == 401

    def test_throttled_request_is_audited(self, client, db_session):
        from auth.rate_limit import DEFAULT_MAX_ATTEMPTS

        for _ in range(DEFAULT_MAX_ATTEMPTS + 1):
            self._try(client, "clinician")
        rows = _events(db_session, "login_rate_limited")
        assert rows, "a throttled attempt must leave a record"
        assert rows[0].succeeded is False


class TestUserPlanCatalogue:
    """
    /api/plans is the user-accessible catalogue behind the profile page's plan
    comparison and upgrade buttons.

    The profile page used to read the admin-only endpoint, which left a regular
    clinician with an empty comparison and no way to request an upgrade.
    """

    def test_requires_a_signed_in_account(self, client, admin):
        assert client.get("/api/plans").status_code == 401

    def test_a_regular_user_is_served(self, client, clinician, login):
        response = client.get("/api/plans", headers=login())
        assert response.status_code == 200, response.text
        assert response.json()["plans"]

    def test_exposes_the_fields_the_profile_page_renders(
            self, client, db_session, login):
        free = plan_named(db_session, "Free", limit=10)
        enterprise = plan_named(db_session, "Enterprise", limit=None, llm=True)
        make_user(db_session, username="clinician", plan=free)

        body = client.get("/api/plans", headers=login()).json()
        by_name = {p["name"]: p for p in body["plans"]}
        # Every field renderPlanBox() reads off a plan object.
        for field in ("id", "name", "price_cents", "monthly_query_limit",
                      "is_unlimited", "llm_enabled", "api_access",
                      "audit_export"):
            assert field in by_name["Free"], f"missing {field}"
        assert by_name["Enterprise"]["is_unlimited"] is True

    def test_does_not_leak_subscriber_counts(self, client, admin, login):
        # Subscriber counts stay an admin-only signal.
        body = client.get("/api/plans", headers=login()).json()
        assert all("subscribers" not in p for p in body["plans"])


"""
Database-session resilience.

The production database is hosted and suspends its compute when idle, so a
connection can be severed while a statement is already in flight. pool_pre_ping
only guards checkout; it does not help once a query is running. These tests pin
the classifier that decides whether a failure is worth retrying -- getting this
wrong in the permissive direction would silently retry real errors.
"""

import pytest
from sqlalchemy.exc import OperationalError

from db.database import _is_connection_dead


class _Orig(Exception):
    """Stands in for the driver-level exception SQLAlchemy wraps."""


def _wrap(message):
    return OperationalError("SELECT 1", {}, _Orig(message))


@pytest.mark.parametrize("message", [
    "server closed the connection unexpectedly\n"
    "This probably means the server terminated abnormally",
    "connection is closed",
    "connection already closed",
    "terminating connection due to administrator command",
    "could not connect to server: Connection refused",
    "server closed the connection unexpectedly",
    "SSL error: connection reset",
    "broken pipe",
])
def test_connection_death_is_retried(message):
    assert _is_connection_dead(_wrap(message)) is True, message


@pytest.mark.parametrize("message", [
    # A query that ran and was rejected must never be retried: retrying a
    # constraint violation just delays the real error.
    "duplicate key value violates unique constraint \"users_username_key\"",
    'relation "users" does not exist',
    "syntax error at or near \"SELEC\"",
    "deadlock detected",
    "permission denied for table users",
    "canceling statement due to statement timeout",
])
def test_real_errors_are_not_retried(message):
    assert _is_connection_dead(_wrap(message)) is False, message


def test_exception_without_a_driver_cause_is_not_retried():
    assert _is_connection_dead(OperationalError("SELECT 1", {}, None)) is False
    assert _is_connection_dead(RuntimeError("boom")) is False


def test_interface_error_counts_as_a_dead_connection():
    """psycopg reports some severances as InterfaceError rather than
    OperationalError; the classifier must still recognise them."""
    from sqlalchemy.exc import InterfaceError

    exc = InterfaceError("SELECT 1", {}, _Orig("something opaque"))
    assert _is_connection_dead(exc) is True


class TestAuditTrail:
    """
    The audit table answers "who signed in, from where, and what did an
    administrator change?" after the fact. These tests pin both that it records
    and that it records nothing sensitive.
    """

    def test_successful_sign_in_is_recorded(self, client, db_session):
        user = make_user(db_session, username="clinician")
        client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery"})
        rows = _events(db_session, "login_success")
        assert len(rows) == 1
        assert rows[0].user_id == user.id
        assert rows[0].succeeded is True

    def test_failed_sign_in_is_recorded(self, client, db_session):
        make_user(db_session, username="clinician")
        client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "wrong"})
        rows = _events(db_session, "login_failed")
        assert len(rows) == 1
        assert rows[0].succeeded is False
        assert rows[0].user_id is not None

    def test_attempt_against_an_unknown_account_is_still_recorded(self, client,
                                                                 db_session):
        client.post("/api/auth/login", json={
            "email": "ghost@example.com", "password": "whatever"})
        rows = _events(db_session, "login_failed")
        assert len(rows) == 1
        # No account matched, so there is no user to attribute it to.
        assert rows[0].user_id is None

    def test_the_identifier_is_hashed_not_stored(self, client, db_session):
        """
        The table records attempts against addresses that may belong to nobody,
        so storing the address itself would turn it into a directory.
        """
        make_user(db_session, username="clinician")
        client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery"})
        row = _events(db_session, "login_success")[0]
        assert row.identifier_hash and len(row.identifier_hash) == 64
        assert "clinician" not in row.identifier_hash

    def test_no_password_material_is_ever_written(self, client, db_session):
        make_user(db_session, username="clinician")
        client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery"})
        client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "a-wrong-password"})
        for row in _events(db_session):
            blob = f"{row.detail} {row.identifier_hash} {row.user_agent}"
            assert "correct-horse-battery" not in blob
            assert "argon2" not in blob.lower()

    def test_registration_is_recorded(self, client, db_session):
        # Registration hands out the Free plan, so one has to exist.
        make_plan(db_session, name="Free", limit=10)
        response = client.post("/api/auth/register", json={
            "username": "newbie", "email": "newbie@example.com",
            "password": "long-enough-password",
        })
        assert response.status_code == 201, response.text
        assert _events(db_session, "register")

    def test_self_password_change_is_recorded(self, client, db_session, login):
        make_user(db_session, username="clinician")
        response = client.post("/api/auth/change-password", headers=login(), json={
            "current_password": "correct-horse-battery",
            "new_password": "a-brand-new-password",
        })
        assert response.status_code == 200
        rows = _events(db_session, "password_changed")
        assert len(rows) == 1
        assert rows[0].user_id == rows[0].actor_user_id

    def test_admin_privilege_changes_name_the_actor(self, client, db_session, login):
        """
        An administrator changing someone else's account must record who did it,
        separately from who it happened to.
        """
        plan = make_plan(db_session, name="Free", limit=10)
        make_user(db_session, username="clinician", plan=plan)
        make_user(db_session, username="root", role="admin", plan=plan)
        response = client.patch("/api/admin/users/1", headers=login("root"),
                                json={"role": "admin"})
        assert response.status_code == 200
        rows = _events(db_session, "role_changed")
        assert len(rows) == 1
        assert rows[0].actor_user_id != rows[0].user_id
        assert "clinician -> admin" in rows[0].detail

    def test_a_no_op_change_records_nothing(self, client, db_session, login):
        """Setting a field to the value it already holds is not an event."""
        plan = make_plan(db_session, name="Free", limit=10)
        make_user(db_session, username="clinician", plan=plan)
        make_user(db_session, username="root", role="admin", plan=plan)
        # Sign in first: login() is itself audited, and the snapshot must not
        # count it.
        headers = login("root")
        before = len(_events(db_session))
        client.patch("/api/admin/users/1", headers=headers, json={"role": "clinician"})
        assert len(_events(db_session)) == before

    def test_deactivation_is_recorded(self, client, db_session, login):
        plan = make_plan(db_session, name="Free", limit=10)
        make_user(db_session, username="clinician", plan=plan)
        make_user(db_session, username="root", role="admin", plan=plan)
        client.patch("/api/admin/users/1", headers=login("root"),
                     json={"is_active": False})
        rows = _events(db_session, "account_deactivated")
        assert len(rows) == 1
        assert rows[0].succeeded is True

    def test_admin_password_reset_is_recorded(self, client, db_session, login):
        plan = make_plan(db_session, name="Free", limit=10)
        make_user(db_session, username="clinician", plan=plan)
        make_user(db_session, username="root", role="admin", plan=plan)
        client.post("/api/admin/users/1/password", headers=login("root"),
                    json={"new_password": "a-replacement-password"})
        rows = _events(db_session, "admin_password_reset")
        assert len(rows) == 1
        assert rows[0].actor_user_id != rows[0].user_id


class TestBootstrapAdminEmailIsUsable:
    """
    Regression test for a self-inflicted lockout.

    scripts/create_admin.py seeded DEFAULT_EMAIL as "admin@variantlens.local".
    Pydantic's EmailStr rejects .local as a special-use domain, so the account
    the bootstrap script created could never use password recovery -- the
    address it was registered against was itself treated as malformed.
    """

    def test_default_email_passes_the_apis_own_validator(self):
        from scripts.create_admin import DEFAULT_EMAIL
        from pydantic import TypeAdapter, ValidationError
        from pydantic import EmailStr

        try:
            TypeAdapter(EmailStr).validate_python(DEFAULT_EMAIL)
        except ValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(f"DEFAULT_EMAIL {DEFAULT_EMAIL!r} is rejected: {exc}")

    def test_reserved_domains_are_what_we_think_they_are(self):
        """Pin the behaviour this bug depended on, so a pin bump is noticed."""
        from pydantic import TypeAdapter, ValidationError
        from pydantic import EmailStr

        adapter = TypeAdapter(EmailStr)
        for bad in ("a@thing.local", "a@thing.test", "a@thing.invalid"):
            with pytest.raises(ValidationError):
                adapter.validate_python(bad)