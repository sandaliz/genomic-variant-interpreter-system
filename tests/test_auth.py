"""
Account-layer tests: registration, login, JWT handling and /api/auth/me.

These run against the in-memory SQLite session from conftest.py, so no
PostgreSQL instance is required.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from auth.security import (
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    create_access_token,
    hash_password,
    verify_password,
)
from db.models import User
from conftest import make_plan, make_user


class TestPasswordHashing:
    def test_hash_is_not_the_plaintext(self):
        digest = hash_password("correct-horse-battery")
        assert "correct-horse-battery" not in digest
        assert digest.startswith("$argon2")

    def test_verify_accepts_right_and_rejects_wrong_password(self):
        digest = hash_password("correct-horse-battery")
        assert verify_password("correct-horse-battery", digest) is True
        assert verify_password("wrong", digest) is False

    def test_verify_survives_a_corrupted_hash(self):
        # A legacy/corrupt hash must read as "wrong password", never a 500.
        assert verify_password("anything", "not-a-real-hash") is False


class TestRegistration:
    def test_register_creates_a_free_plan_user(self, client, db_session):
        make_plan(db_session, name="Free", limit=3)
        response = client.post("/api/auth/register", json={
            "username": "newclinician",
            "email": "New.Clinician@Example.com",
            "password": "a-long-enough-password",
        })
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["username"] == "newclinician"
        # Email is normalised to lower case so lookups are unambiguous.
        assert body["email"] == "new.clinician@example.com"
        assert "password" not in body and "password_hash" not in body

    def test_self_registration_can_never_create_an_admin(self, client, db_session):
        make_plan(db_session, name="Free", limit=3)
        response = client.post("/api/auth/register", json={
            "username": "sneaky",
            "email": "sneaky@example.com",
            "password": "a-long-enough-password",
            "role": "admin",
        })
        assert response.status_code == 201
        assert response.json()["role"] == "clinician"

    @pytest.mark.parametrize("payload,reason", [
        ({"username": "ab", "email": "a@example.com", "password": "longenough1"},
         "username below the 3-character minimum"),
        ({"username": "valid", "email": "not-an-email", "password": "longenough1"},
         "email is not an email address"),
        ({"username": "valid", "email": "a@example.com", "password": "short"},
         "password below the 8-character minimum"),
        ({"username": "bad user!", "email": "a@example.com", "password": "longenough1"},
         "username contains characters outside the allowed set"),
    ])
    def test_invalid_registration_is_rejected(self, client, db_session, payload, reason):
        make_plan(db_session, name="Free", limit=3)
        assert client.post("/api/auth/register", json=payload).status_code == 422, reason

    def test_duplicate_username_conflicts(self, client, db_session):
        plan = make_plan(db_session, name="Free", limit=3)
        make_user(db_session, username="clinician", plan=plan)
        response = client.post("/api/auth/register", json={
            "username": "clinician",
            "email": "other@example.com",
            "password": "a-long-enough-password",
        })
        assert response.status_code == 409

    def test_duplicate_email_conflicts(self, client, db_session):
        plan = make_plan(db_session, name="Free", limit=3)
        make_user(db_session, username="clinician", plan=plan)
class TestLogin:
    def test_login_returns_a_token_and_the_public_user(self, client, db_session):
        make_user(db_session, username="clinician")
        response = client.post("/api/auth/login", json={
            "username": "clinician", "password": "correct-horse-battery",
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["username"] == "clinician"
        claims = jwt.decode(body["access_token"], JWT_SECRET_KEY,
                            algorithms=[JWT_ALGORITHM])
        assert claims["sub"] == str(body["user"]["id"])

    def test_wrong_password_is_401(self, client, db_session):
        make_user(db_session, username="clinician")
        response = client.post("/api/auth/login", json={
            "username": "clinician", "password": "not-the-password",
        })
        assert response.status_code == 401

    def test_unknown_user_is_401_with_the_same_message(self, client, db_session):
        make_user(db_session, username="clinician")
        unknown = client.post("/api/auth/login", json={
            "username": "nobody", "password": "correct-horse-battery",
        })
        wrong = client.post("/api/auth/login", json={
            "username": "clinician", "password": "not-the-password",
        })
        # Identical responses stop an attacker enumerating valid usernames.
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_deactivated_account_cannot_log_in(self, client, db_session):
        make_user(db_session, username="clinician", is_active=False)
        response = client.post("/api/auth/login", json={
            "username": "clinician", "password": "correct-horse-battery",
        })
        assert response.status_code == 401
        assert "deactivated" in response.json()["detail"].lower()


def _token_for(user, **overrides):
    """Mint a valid token for a user row, optionally overriding claims."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=30),
    }
    claims.update(overrides)
    return jwt.encode(claims, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


class TestProtectedRoutes:
    def test_me_requires_a_token(self, client, db_session):
        make_user(db_session)
        assert client.get("/api/auth/me").status_code == 401

    def test_me_reports_plan_usage_and_entitlements(self, client, db_session, login):
        plan = make_plan(db_session, name="Professional", limit=50, llm=True,
                         audit_export=True, price_cents=4900)
        make_user(db_session, username="clinician", plan=plan)
        response = client.get("/api/auth/me", headers=login())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plan"]["name"] == "Professional"
        assert body["plan"]["price_cents"] == 4900
        assert body["usage"] == {"used_this_month": 0, "monthly_limit": 50,
                                 "remaining": 50}
        assert body["entitlements"] == {"llm_enabled": True, "api_access": True,
                                        "audit_export": True}

    def test_expired_token_is_rejected(self, client, db_session):
        user = make_user(db_session, username="clinician")
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        token = _token_for(user, iat=past - timedelta(hours=1), exp=past)
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_token_signed_with_another_key_is_rejected(self, client, db_session):
        user = make_user(db_session, username="clinician")
        token = jwt.encode(
            {"sub": str(user.id), "username": user.username, "role": "admin",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            "a-different-signing-key",
            algorithm=JWT_ALGORITHM,
        )
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_role_in_the_token_is_not_trusted(self, client, db_session):
        """A clinician cannot become an admin by putting role=admin in a token."""
        user = make_user(db_session, username="clinician")
        token = jwt.encode(
            {"sub": str(user.id), "username": user.username, "role": "admin",
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET_KEY,
            algorithm=JWT_ALGORITHM,
        )
        headers = {"Authorization": f"Bearer {token}"}
        # The endpoint still works, but the database role (clinician) is used.
        me = client.get("/api/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["user"]["role"] == "clinician"
        assert client.get("/api/admin/users", headers=headers).status_code == 403

    def test_deactivation_takes_effect_on_an_unexpired_token(self, client, db_session, login):
        make_user(db_session, username="clinician")
        headers = login()
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        # The token is still cryptographically valid and unexpired...
        user = db_session.query(User).filter_by(username="clinician").one()
        user.is_active = False
        db_session.commit()

        # ...but identity is re-read per request, so access is revoked instantly.
        response = client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert "deactivated" in response.json()["detail"].lower()

    def test_token_for_a_deleted_account_is_rejected(self, client, db_session, login):
        make_user(db_session, username="clinician")
        headers = login()
        user = db_session.query(User).filter_by(username="clinician").one()
        db_session.delete(user)
        db_session.commit()
        response = client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert "no longer exists" in response.json()["detail"].lower()

    @pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "not-a-jwt"])
    def test_malformed_authorization_header_is_401(self, client, db_session, header):
        make_user(db_session)
        response = client.get("/api/auth/me", headers={"Authorization": header})
        assert response.status_code == 401, header


class TestTokenContents:
    def test_token_carries_no_sensitive_claims(self):
        token = create_access_token(user_id=7, username="dr_house", role="clinician")
        claims = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        assert claims["sub"] == "7"
        assert claims["role"] == "clinician"
        # No email, no plan, no password material in the token.
        assert "email" not in claims
        assert "plan" not in claims
        assert "password" not in str(claims)