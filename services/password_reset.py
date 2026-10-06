"""
services/password_reset.py

Issue and redeem one-time password-reset codes.

Security properties this module is responsible for:
  * the code is never stored in plaintext -- only a salted SHA-256 digest;
  * a code is single use, expires after RESET_CODE_TTL_MINUTES, and is capped
    at RESET_CODE_MAX_ATTEMPTS guesses;
  * issuing a code invalidates every earlier live code for that account, so an
    old code cannot be redeemed after a newer request;
  * requesting a code never reveals whether the address exists.

SHA-256 is the right primitive here precisely because the input is a 6-digit
code from a small keyspace: this is not a password, it is a low-entropy token
that is time-limited and attempt-limited. Password hashing uses Argon2id instead
-- see auth/security.py.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PasswordReset, User

# Six digits is what a person can read off a screen and type back. The attempt
# cap below is what makes that acceptable: 1e6 possibilities / 5 attempts is
# useless to a brute-forcer, and the code dies in 10 minutes regardless.
CODE_DIGITS = 6
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """
    Normalise a stored datetime to timezone-aware UTC.

    PostgreSQL returns aware timestamps for DateTime(timezone=True), but SQLite
    hands back naive ones. Comparing the two raises, so every comparison in this
    module goes through here rather than assuming one dialect's behaviour.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def generate_code() -> str:
    """A cryptographically random numeric code, zero-padded to CODE_DIGITS."""
    return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))


def _new_salt() -> str:
    return secrets.token_hex(16)


def hash_code(code: str, salt: str) -> str:
    """Salted SHA-256 hex digest of a reset code."""
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def verify_code(code: str, salt: str, expected_hash: str) -> bool:
    """Constant-time comparison of a submitted code against the stored digest."""
    return secrets.compare_digest(hash_code(code.strip(), salt), expected_hash)


def issue_code(db: Session, user: User) -> Tuple[PasswordReset, str]:
    """
    Issue a fresh code for a user, superseding any earlier live ones.

    Returns the row and the plaintext code. The caller is responsible for
    delivering (or, in development, echoing) the plaintext -- it is not
    recoverable afterwards.
    """
    # Supersede first: only the most recent request can ever be redeemed.
    for existing in db.scalars(
        select(PasswordReset).where(
            PasswordReset.user_id == user.id,
            PasswordReset.consumed_at.is_(None),
        )
    ).all():
        existing.consumed_at = _now()

    code = generate_code()
    salt = _new_salt()
    row = PasswordReset(
        user_id=user.id,
        email=user.email,
        salt=salt,
        code_hash=hash_code(code, salt),
        attempts=0,
        expires_at=_now() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, code


def find_live_code(db: Session, user: User) -> Optional[PasswordReset]:
    """The most recent unused, unexpired code for a user, or None."""
    return db.scalars(
        select(PasswordReset)
        .where(
            PasswordReset.user_id == user.id,
            PasswordReset.consumed_at.is_(None),
        )
        .order_by(PasswordReset.created_at.desc())
        .limit(1)
    ).one_or_none()


def redeem_code(db: Session, user: User, code: str) -> Tuple[bool, str]:
    """
    Attempt to redeem a code.

    Returns (ok, message). The message is deliberately vague on failure -- the
    caller must not be able to distinguish "wrong code" from "expired" from
    "already used", because that difference would let an attacker probe which
    codes are live.
    """
    generic_failure = "That verification code is not valid or has expired."

    row = find_live_code(db, user)
    if row is None:
        return False, generic_failure

    if as_utc(row.expires_at) <= _now():
        row.consumed_at = _now()  # burn it so it cannot be retried later
        db.commit()
        return False, generic_failure

    if row.attempts >= CODE_MAX_ATTEMPTS:
        row.consumed_at = _now()  # cap reached: burn the code
        db.commit()
        return False, generic_failure

    if not verify_code(code, row.salt, row.code_hash):
        # Count the attempt and commit -- a wrong guess must not be free.
        row.attempts += 1
        db.commit()
        return False, generic_failure

    # Single use: stamp it now so a replay finds nothing.
    row.consumed_at = _now()
    db.commit()
    return True, "Code accepted."