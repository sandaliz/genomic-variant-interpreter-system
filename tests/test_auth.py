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
    JWT_AUDIENCE,
    JWT_ISSUER,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
    _ENCRYPTION_KEY,
    _SIGNING_KEY,
)
from auth.security import _decrypt as _jwe_decrypt
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
            "email": "clinician@example.com", "password": "correct-horse-battery",
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["username"] == "clinician"
        claims = decode_access_token(body["access_token"])
        assert claims["sub"] == str(body["user"]["id"])

    def test_wrong_password_is_401(self, client, db_session):
        make_user(db_session, username="clinician")
        response = client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "not-the-password",
        })
        assert response.status_code == 401

    def test_unknown_user_is_401_with_the_same_message(self, client, db_session):
        make_user(db_session, username="clinician")
        unknown = client.post("/api/auth/login", json={
            "email": "nobody@example.com", "password": "correct-horse-battery",
        })
        wrong = client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "not-the-password",
        })
        # Identical responses stop an attacker enumerating valid usernames.
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_deactivated_account_cannot_log_in(self, client, db_session):
        make_user(db_session, username="clinician", is_active=False)
        response = client.post("/api/auth/login", json={
            "email": "clinician@example.com", "password": "correct-horse-battery",
        })
        assert response.status_code == 401
        assert "deactivated" in response.json()["detail"].lower()


def _mint(claims, signing_key=None, encryption_key=None):
    """
    Build a token the way the app does: sign with the HMAC key, then wrap it in
    the JWE envelope. The key arguments are the seam the security tests use to
    forge tokens with the wrong key material.
    """
    import json

    from jwcrypto import jwe

    signed = jwt.encode(claims, signing_key or _SIGNING_KEY,
                        algorithm=JWT_ALGORITHM)
    envelope = jwe.JWE(
        signed.encode("utf-8"),
        json.dumps({"alg": "dir", "enc": "A256GCM", "cty": "JWT"}),
        recipient=encryption_key or _ENCRYPTION_KEY,
    )
    return envelope.serialize(compact=True)


def _claims_for(user, **overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=30),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    claims.update(overrides)
    return claims


def _token_for(user, **overrides):
    """Mint a valid token for a user row, optionally overriding claims."""
    return _mint(_claims_for(user, **overrides))


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
        token = _mint(
            _claims_for(user, role="admin",
                        exp=datetime.now(timezone.utc) + timedelta(minutes=5)),
            signing_key=b"a-different-signing-key",
        )
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_token_encrypted_with_another_key_is_rejected(self, client, db_session):
        """The envelope key is checked before the signature ever gets a look-in."""
        from jwcrypto import jwk

        user = make_user(db_session, username="clinician")
        token = _mint(
            _claims_for(user, exp=datetime.now(timezone.utc) + timedelta(minutes=5)),
            encryption_key=jwk.JWK.generate(kty="oct", size=256),
        )
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_unsigned_three_part_token_is_rejected(self, client, db_session):
        """
        A correctly-signed but unencrypted token is refused: the confidentiality
        property must not be bypassable by simply dropping the JWE envelope.
        """
        user = make_user(db_session, username="clinician")
        bare = jwt.encode(
            _claims_for(user,
                        exp=datetime.now(timezone.utc) + timedelta(minutes=5)),
            _SIGNING_KEY, algorithm=JWT_ALGORITHM,
        )
        assert len(bare.split(".")) == 3
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {bare}"})
        assert response.status_code == 401
        assert "encrypted form" in response.json()["detail"].lower()

    def test_token_for_another_audience_is_rejected(self, client, db_session):
        """A valid signature is not enough if the token was minted elsewhere."""
        user = make_user(db_session, username="clinician")
        token = _token_for(user, aud="some-other-service")
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_token_from_another_issuer_is_rejected(self, client, db_session):
        user = make_user(db_session, username="clinician")
        token = _token_for(user, iss="some-other-issuer")
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_token_missing_aud_is_rejected(self, client, db_session):
        user = make_user(db_session, username="clinician")
        claims = _claims_for(user)
        claims.pop("aud")
        token = _mint(claims)
        response = client.get("/api/auth/me",
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_tampered_ciphertext_is_rejected(self, client, db_session):
        """GCM authenticates the ciphertext, so a flipped character fails."""
        user = make_user(db_session, username="clinician")
        token = _token_for(user, role="admin")
        parts = token.split(".")
        assert len(parts) == 5
        # Corrupt the ciphertext segment, leaving the tag intact so the failure
        # cannot be dismissed as a malformed blob.
        segment = parts[3]
        parts[3] = segment[:-4] + ("AAAA" if segment[-4:] != "AAAA" else "BBBB")
        response = client.get(
            "/api/auth/me", headers={"Authorization": "Bearer " + ".".join(parts)})
        assert response.status_code == 401

    def test_role_in_the_token_is_not_trusted(self, client, db_session):
        """A clinician cannot become an admin by putting role=admin in a token."""
        user = make_user(db_session, username="clinician")
        token = _token_for(user, role="admin")
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
        claims = decode_access_token(token)
        assert claims["sub"] == "7"
        assert claims["role"] == "clinician"
        # No email, no plan, no password material in the token.
        assert "email" not in claims
        assert "plan" not in claims
        assert "password" not in str(claims)

    def test_token_is_encrypted_on_the_wire(self):
        """
        The whole point of the JWE envelope: the claims must not be readable
        without the decryption key, even though the holder could always ask the
        server who they are.
        """
        token = create_access_token(
            user_id=7, username="dr_house", role="clinician")
        assert len(token.split(".")) == 5, "expected a 5-part compact JWE"
        # Base64-decoding any segment must not reveal the username or the role.
        import base64

        for segment in token.split(".")[:1] + token.split(".")[2:4]:
            padded = segment + "=" * (-len(segment) % 4)
            try:
                raw = base64.urlsafe_b64decode(padded)
            except Exception:
                continue
            assert b"dr_house" not in raw
            assert b"clinician" not in raw

    def test_encryption_key_is_not_the_signing_key(self):
        """
        Reusing one key for signing and encryption turns a signing-key leak into
        a decryption leak as well, so the two must derive to different material.
        """
        from auth.security import _ENC_KEY_BYTES, _SIGNING_KEY

        assert bytes(_SIGNING_KEY) != _ENC_KEY_BYTES
        # Same configured secret must not collapse both onto one key either.
        from auth.security import _HKDF_ENC_INFO, _HKDF_SIGN_INFO, _derive_key

        assert _derive_key("same-secret", _HKDF_SIGN_INFO) != _derive_key(
            "same-secret", _HKDF_ENC_INFO
        )