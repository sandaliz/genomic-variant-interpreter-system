"""
auth/security.py

Password hashing (Argon2id) and JWT access tokens.

This module sits in front of the four-agent pipeline: it decides WHO a caller is
before any agent sees a query. It deliberately knows nothing about the database --
callers pass in values they have already loaded.

Environment:
    JWT_SECRET_KEY       required in any real deployment; a clearly-labelled
                         development fallback is used only when it is unset
    JWT_ALGORITHM        default HS256
    JWT_EXPIRE_MINUTES   default 60
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from pwdlib import PasswordHash

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Argon2id via the pwdlib recommended configuration. Plaintext passwords are
# never stored, logged or returned by the API.
_password_hash = PasswordHash.recommended()

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# Development fallback only. Any real deployment must set JWT_SECRET_KEY; the
# flag below lets callers surface that fact rather than hiding it.
_DEV_SECRET = "variantlens-development-secret-do-not-use-in-production"
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or _DEV_SECRET
USING_DEV_SECRET = JWT_SECRET_KEY == _DEV_SECRET


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or wrongly signed."""


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against its stored hash (constant time)."""
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:
        # A malformed or legacy hash must read as wrong password, never a 500.
        return False


def create_access_token(user_id: int, username: str, role: str) -> str:
    """
    Mint a short-lived access token.

    Only identity and role go into the token -- never the email, the plan or any
    other sensitive value. Authorization details are re-read from the database on
    every request (see auth/dependencies.py).
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a token, raising TokenError on any problem."""
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid or malformed token.") from exc
