"""
services/api_key_service.py

Issue, resolve and revoke API keys for programmatic access.

An API key is the machine-client counterpart of a session token: short-lived
tokens are wrong for a LIMS integration that runs unattended, so a plan whose
``api_access`` entitlement is on may mint long-lived keys instead.

Storage mirrors the password-reset codes on purpose -- the plaintext key is shown
once at issue time and only its SHA-256 digest is persisted, so a database leak
does not hand out working credentials. Resolution is a single indexed lookup on
that digest; the raw key is never stored, logged or recoverable.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ApiKey, User

# Visible prefix so a key is recognisable in logs and the UI without being usable.
KEY_PREFIX = "vl_"
_RAW_BYTES = 32  # -> 43 url-safe characters, ~256 bits of entropy


def _digest(raw: str) -> str:
    """SHA-256 hex digest of a presented key. Deterministic, so it is indexable."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_key(db: Session, user: User, name: str) -> tuple[ApiKey, str]:
    """
    Mint a new key for an account. Returns (row, plaintext).

    The plaintext is returned exactly once and never stored -- the caller must
    show it to the user now, because it cannot be reproduced afterwards.
    """
    raw = KEY_PREFIX + secrets.token_urlsafe(_RAW_BYTES)
    row = ApiKey(
        user_id=user.id,
        name=name.strip(),
        key_hash=_digest(raw),
        prefix=raw[: len(KEY_PREFIX) + 8],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw


def resolve_key(db: Session, raw: Optional[str]) -> Optional[User]:
    """
    Return the account a presented key authenticates, or None.

    None for every failure mode (unknown, revoked, inactive account, or a plan
    that no longer includes API access) -- the caller turns all of them into a
    single 401, so the response cannot be used to probe which keys exist.
    """
    if not raw or not isinstance(raw, str):
        return None
    row = db.scalars(select(ApiKey).where(ApiKey.key_hash == _digest(raw))).one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    user = db.scalars(select(User).where(User.id == row.user_id)).one_or_none()
    if user is None or not user.is_active:
        return None
    # A key outlives a downgrade. Checking the entitlement here, not only at
    # issue time, means moving off a paid plan revokes programmatic access
    # immediately rather than leaving a working credential behind.
    if not user.plan.api_access:
        return None

    row.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return user


def keys_for(db: Session, user: User) -> list[ApiKey]:
    """The account's keys, newest first (revoked ones included, for the record)."""
    return list(db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id.desc())
    ).all())


def revoke_key(db: Session, user: User, key_id: int) -> bool:
    """
    Revoke one of the account's own keys. Returns False if it is not theirs.

    Scoped by user_id rather than looked up by id alone, so one account can never
    revoke another's key by guessing an id.
    """
    row = db.scalars(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    ).one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True
