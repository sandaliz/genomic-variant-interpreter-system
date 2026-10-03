"""
Tests for login-by-email and the password-reset flow.

The reset flow is where an implementation most easily leaks: through an
enumeration response, a replayed code, a stored plaintext code, or an attempt
budget that never depletes. Each of those has a test here.
"""

from datetime import timedelta

import pytest

from db.models import PasswordReset
from services.password_reset import (
    CODE_MAX_ATTEMPTS,
    CODE_TTL_MINUTES,
    generate_code,
    hash_code,
    verify_code,
)
from conftest import make_plan, make_user


PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def user(db_session):
    plan = make_plan(db_session, name="Free", limit=5)
    return make_user(db_session, username="alice", plan=plan, password=PASSWORD)


class TestLoginByEmail:
    def test_login_with_username(self, client, user):
        response = client.post("/api/auth/login",
                               json={"username": "alice", "password": PASSWORD})
        assert response.status_code == 200, response.text

    def test_login_with_email(self, client, user):
        response = client.post("/api/auth/login",
                               json={"username": "alice@example.com",
                                     "password": PASSWORD})
        assert response.status_code == 200, response.text
        assert response.json()["user"]["username"] == "alice"

    def test_email_login_is_case_insensitive(self, client, user):
        response = client.post("/api/auth/login",
                               json={"username": "ALICE@Example.COM",
                                     "password": PASSWORD})
        assert response.status_code == 200, response.text

    def test_wrong_password_with_email_is_401(self, client, user):
        response = client.post("/api/auth/login",
                               json={"username": "alice@example.com",
                                     "password": "nope"})
        assert response.status_code == 401

    def test_unknown_identifier_is_401_with_the_same_message(self, client, user):
        unknown = client.post("/api/auth/login",
                              json={"username": "nobody@example.com",
                                    "password": PASSWORD})
        wrong = client.post("/api/auth/login",
                            json={"username": "alice", "password": "nope"})
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]


class TestResetCodeStorage:
    def test_code_is_never_stored_in_plaintext(self, client, user, db_session):
        client.post("/api/auth/password-reset/request", json={"email": user.email})
        row = db_session.query(PasswordReset).one()
        # Neither the digest nor the salt may look like the 6-digit code.
        assert row.code_hash and row.salt
        assert not (row.code_hash.isdigit() and len(row.code_hash) == 6)
class TestResetRequest:
    def test_returns_the_code_in_log_mode(self, client, user):
        """MAIL_DELIVERY defaults to log so the flow is testable locally."""
        response = client.post("/api/auth/password-reset/request",
                               json={"email": user.email})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["delivery"] == "log"
        assert body["dev_code"] is not None and len(body["dev_code"]) == 6

    def test_unknown_email_gets_the_same_message(self, client, user):
        """Otherwise this endpoint confirms which addresses are registered."""
        known = client.post("/api/auth/password-reset/request",
                            json={"email": user.email}).json()
        unknown = client.post("/api/auth/password-reset/request",
                              json={"email": "nobody@example.com"}).json()
        assert known["message"] == unknown["message"]
        # Only the code differs, and only because this is development mode.
        assert unknown["dev_code"] is None

    def test_unknown_email_issues_no_row(self, client, user, db_session):
        client.post("/api/auth/password-reset/request",
                    json={"email": "nobody@example.com"})
        assert db_session.query(PasswordReset).count() == 0

    def test_deactivated_account_issues_no_row(self, client, user, db_session):
        user.is_active = False
        db_session.commit()
        response = client.post("/api/auth/password-reset/request",
                               json={"email": user.email})
        assert response.status_code == 200
        assert response.json()["dev_code"] is None
        assert db_session.query(PasswordReset).count() == 0

    def test_a_new_request_supersedes_the_previous_code(self, client, user,
                                                         db_session):
        """An older code must not remain redeemable after a newer request."""
        first = client.post("/api/auth/password-reset/request",
                            json={"email": user.email}).json()["dev_code"]
        second = client.post("/api/auth/password-reset/request",
                             json={"email": user.email}).json()["dev_code"]
        rows = db_session.query(PasswordReset).all()
        assert len(rows) == 2
        assert any(r.consumed_at is not None for r in rows)

        old = client.post("/api/auth/password-reset/confirm",
                          json={"email": user.email, "code": first,
                                "new_password": NEW_PASSWORD})
        assert old.status_code == 400
        newest = client.post("/api/auth/password-reset/confirm",
                             json={"email": user.email, "code": second,
                                   "new_password": NEW_PASSWORD})
        assert newest.status_code == 200


class TestResetConfirm:
    def test_happy_path_sets_the_password_and_signs_in(self, client, user):
        code = client.post("/api/auth/password-reset/request",
                           json={"email": user.email}).json()["dev_code"]
        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": code,
                                     "new_password": NEW_PASSWORD})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["username"] == "alice"
        assert body["access_token"]

        # The new password works, the old one does not.
        assert client.post("/api/auth/login",
                           json={"username": "alice",
                                 "password": NEW_PASSWORD}).status_code == 200
        assert client.post("/api/auth/login",
                           json={"username": "alice",
                                 "password": PASSWORD}).status_code == 401

    def test_code_is_single_use(self, client, user):
        code = client.post("/api/auth/password-reset/request",
                           json={"email": user.email}).json()["dev_code"]
        assert client.post("/api/auth/password-reset/confirm",
                           json={"email": user.email, "code": code,
                                 "new_password": NEW_PASSWORD}).status_code == 200
        replay = client.post("/api/auth/password-reset/confirm",
                             json={"email": user.email, "code": code,
                                   "new_password": "yet-another-password"})
        assert replay.status_code == 400

    def test_wrong_code_is_400(self, client, user):
        client.post("/api/auth/password-reset/request", json={"email": user.email})
        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": "000000",
                                     "new_password": NEW_PASSWORD})
        assert response.status_code == 400

    def test_malformed_code_is_422(self, client, user):
        client.post("/api/auth/password-reset/request", json={"email": user.email})
        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": "abc",
                                     "new_password": NEW_PASSWORD})
        assert response.status_code == 422

    def test_short_new_password_is_422(self, client, user):
        code = client.post("/api/auth/password-reset/request",
                           json={"email": user.email}).json()["dev_code"]
        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": code,
                                     "new_password": "short"})
        assert response.status_code == 422

    def test_expired_code_is_rejected(self, client, user, db_session):
        code = client.post("/api/auth/password-reset/request",
                           json={"email": user.email}).json()["dev_code"]
        row = db_session.query(PasswordReset).one()
        row.expires_at = row.expires_at - timedelta(minutes=CODE_TTL_MINUTES + 1)
        db_session.commit()

        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": code,
                                     "new_password": NEW_PASSWORD})
        assert response.status_code == 400
        assert "expired" in response.json()["detail"].lower()

    def test_attempts_are_capped(self, client, user, db_session):
        """A 6-digit code must not be brute-forceable through the endpoint."""
        real = client.post("/api/auth/password-reset/request",
                           json={"email": user.email}).json()["dev_code"]
        for _ in range(CODE_MAX_ATTEMPTS):
            wrong = "000000" if real != "000000" else "111111"
            assert client.post("/api/auth/password-reset/confirm",
                               json={"email": user.email, "code": wrong,
                                     "new_password": NEW_PASSWORD}).status_code == 400
        row = db_session.query(PasswordReset).one()
        assert row.attempts == CODE_MAX_ATTEMPTS
        # The correct code is now refused too: the budget is spent.
        assert client.post("/api/auth/password-reset/confirm",
                           json={"email": user.email, "code": real,
                                 "new_password": NEW_PASSWORD}).status_code == 400

    def test_unknown_email_is_400_with_the_generic_message(self, client, user):
        response = client.post("/api/auth/password-reset/confirm",
                               json={"email": "nobody@example.com",
                                     "code": "123456",
                                     "new_password": NEW_PASSWORD})
        assert response.status_code == 400
        assert "not valid" in response.json()["detail"].lower()

    def test_deactivated_account_cannot_reset(self, client, user, db_session):
        user.is_active = False
        db_session.commit()
        assert client.post("/api/auth/password-reset/confirm",
                           json={"email": user.email, "code": "123456",
                                 "new_password": NEW_PASSWORD}).status_code == 400


class TestFullName:
    def test_register_defaults_to_no_full_name(self, client, db_session):
        make_plan(db_session, name="Free", limit=5)
        response = client.post("/api/auth/register", json={
            "username": "newbie", "email": "newbie@example.com",
            "password": "a-long-enough-password"})
        assert response.status_code == 201
        assert response.json()["full_name"] is None

    def test_hash_round_trips(self):
        salt = "abc123"
        digest = hash_code("123456", salt)
        assert verify_code("123456", salt, digest) is True
        assert verify_code("654321", salt, digest) is False
        # A different salt must not verify against the same digest.
        assert verify_code("123456", "different", digest) is False

    def test_generated_codes_are_six_digits(self):
        for _ in range(20):
            code = generate_code()
            assert len(code) == 6 and code.isdigit()