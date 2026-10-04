"""
Profile tests: editing your own details, and changing your own password.

The security-relevant cases are the email change (which controls where password
recovery goes) and proving ownership of the account before a credential change.
"""

import pytest

from db.models import User
from conftest import make_plan, make_user


PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-different-password"


@pytest.fixture
def user(db_session):
    """Named 'clinician' so the shared login() fixture signs in as this account."""
    plan = make_plan(db_session, name="Free", limit=5)
    return make_user(db_session, username="clinician", plan=plan, password=PASSWORD)


class TestUpdateProfile:
    def test_requires_authentication(self, client, user):
        assert client.patch("/api/auth/me", json={"full_name": "X"}).status_code == 401

    def test_full_name_can_be_set(self, client, user, login):
        response = client.patch("/api/auth/me", json={"full_name": "Alice Carter"},
                                headers=login())
        assert response.status_code == 200, response.text
        assert response.json()["user"]["full_name"] == "Alice Carter"

    def test_full_name_is_stripped_and_blank_clears_it(self, client, user, login,
                                                       db_session):
        client.patch("/api/auth/me", json={"full_name": "  Alice  "}, headers=login())
        assert client.get("/api/auth/me", headers=login()).json()[
            "user"]["full_name"] == "Alice"
        client.patch("/api/auth/me", json={"full_name": "   "}, headers=login())
        assert client.get("/api/auth/me", headers=login()).json()[
            "user"]["full_name"] is None

    def test_response_keeps_the_full_me_payload(self, client, user, login):
        """The profile page gets plan + usage back in the same call."""
        body = client.patch("/api/auth/me", json={"full_name": "Alice"},
                            headers=login()).json()
        assert set(body) >= {"user", "plan", "usage", "entitlements"}
        assert body["plan"]["name"] == "Free"
        assert body["usage"]["monthly_limit"] == 5

    def test_email_change_requires_the_current_password(self, client, user, login):
        response = client.patch("/api/auth/me", json={
            "email": "new@example.com", "current_password": "wrong"}, headers=login())
        assert response.status_code == 403

    def test_email_change_without_a_password_is_refused(self, client, user, login):
        response = client.patch("/api/auth/me", json={"email": "new@example.com"},
                                headers=login())
        assert response.status_code == 403
        assert "current password" in response.json()["detail"].lower()

    def test_email_change_succeeds_with_the_password(self, client, user, login):
        response = client.patch("/api/auth/me", json={
            "email": "New@Example.COM", "current_password": PASSWORD},
            headers=login())
        assert response.status_code == 200, response.text
        assert response.json()["user"]["email"] == "new@example.com"
        # The new address is now a valid sign-in identifier.
        assert client.post("/api/auth/login",
                           json={"email": "new@example.com",
                                 "password": PASSWORD}).status_code == 200

    def test_email_change_to_a_taken_address_conflicts(self, client, db_session,
                                                       user, login):
        plan = db_session.get(type(user.plan), user.plan_id)
        make_user(db_session, username="bob", plan=plan)
        response = client.patch("/api/auth/me", json={
            "email": "bob@example.com", "current_password": PASSWORD},
            headers=login())
        assert response.status_code == 409

    def test_setting_the_same_email_is_a_no_op(self, client, user, login):
        response = client.patch("/api/auth/me", json={
            "email": "clinician@example.com", "current_password": "wrong"},
            headers=login())
        # No change requested, so no password proof is demanded.
        assert response.status_code == 200


class TestChangePassword:
    def test_requires_authentication(self, client, user):
        assert client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD}
        ).status_code == 401

    def test_wrong_current_password_is_403(self, client, user, login):
        response = client.post("/api/auth/change-password", json={
            "current_password": "not-it", "new_password": NEW_PASSWORD},
            headers=login())
        assert response.status_code == 403
        assert "incorrect" in response.json()["detail"].lower()

    def test_change_works_and_old_password_stops(self, client, user, login):
        response = client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=login())
        assert response.status_code == 200
        assert response.json()["changed"] is True
        assert client.post("/api/auth/login",
                           json={"email": "clinician@example.com",
                                 "password": NEW_PASSWORD}).status_code == 200
        assert client.post("/api/auth/login",
                           json={"email": "clinician@example.com",
                                 "password": PASSWORD}).status_code == 401

    def test_reusing_the_same_password_is_rejected(self, client, user, login):
        response = client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": PASSWORD},
            headers=login())
        assert response.status_code == 422
        assert "different" in response.json()["detail"].lower()

    def test_short_new_password_is_422(self, client, user, login):
        assert client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": "short"},
            headers=login()).status_code == 422

    def test_response_never_echoes_credentials(self, client, user, login):
        raw = client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=login()).text
        assert NEW_PASSWORD not in raw
        assert PASSWORD not in raw
        assert "argon2" not in raw.lower()

    def test_a_session_survives_its_own_password_change(self, client, user, login):
        """Documented behaviour: a credential change does not revoke live tokens."""
        headers = login()
        client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=headers)
        assert client.get("/api/auth/me", headers=headers).status_code == 200

    def test_deactivated_account_cannot_change_its_password(self, client, user,
                                                            login, db_session):
        headers = login()
        db_session.query(User).filter_by(username="clinician").one().is_active = False
        db_session.commit()
        assert client.post("/api/auth/change-password", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=headers).status_code == 401


class TestProfileCannotEscalate:
    def test_username_cannot_be_changed(self, client, user, login, db_session):
        """Renaming would rewrite identity in tokens and the audit trail."""
        response = client.patch("/api/auth/me", json={"username": "mallory"},
                                headers=login())
        assert response.status_code == 422
        db_session.refresh(user)
        assert user.username == "clinician"

    def test_unknown_fields_are_rejected(self, client, user, login):
        """A typo must not look like it worked."""
        assert client.patch("/api/auth/me", json={"is_admin": True},
                            headers=login()).status_code == 422

    def test_password_is_never_returned(self, client, user, login):
        raw = client.patch("/api/auth/me", json={"full_name": "Alice"},
                           headers=login()).text
        assert "argon2" not in raw.lower()
        assert "password" not in raw.lower()