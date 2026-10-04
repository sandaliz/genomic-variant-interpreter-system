"""
auth/audit.py

Records security-relevant actions in the auth_events table.

Design rule: **auditing must never break the operation it describes.** A failed
write is swallowed and reported to the log stream, because an audit insert that
can turn a successful sign-in into a 500 would be a denial-of-service primitive
in its own right. Anything that genuinely depends on the audit record being
durable belongs in the same transaction as the change, not here.
"""

import hashlib
import logging

from db.models import AuthEvent

logger = logging.getLogger(__name__)

# Keeps the detail column bounded so a long user agent cannot overflow it.
_MAX_AGENT = 256
_MAX_DETAIL = 500


def hash_identifier(identifier: str | None) -> str | None:
    """
    Digest the identifier a caller typed.

    Salted with the JWT signing key on purpose: a plain SHA-256 of an email
    address is trivially reversed by hashing a wordlist of addresses, and this
    table records attempts against addresses that may belong to nobody. Using the
    signing key means the digest cannot be computed without also holding a secret
    the database itself never receives.
    """
    if not identifier:
        return None
    from auth.security import _SIGNING_KEY

    salt = bytes(_SIGNING_KEY)
    return hashlib.sha256(salt + identifier.strip().lower().encode("utf-8")).hexdigest()


def record(
    db,
    event_type: str,
    *,
    user_id=None,
    actor_user_id=None,
    succeeded: bool = True,
    identifier: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    detail: str | None = None,
) -> AuthEvent | None:
    """
    Append one event. Returns the row, or None if it could not be written.
    """
    row = AuthEvent(
        event_type=event_type,
        user_id=user_id,
        actor_user_id=actor_user_id,
        succeeded=bool(succeeded),
        identifier_hash=hash_identifier(identifier),
        client_ip=(client_ip or None) and str(client_ip)[:64],
        user_agent=(user_agent or None) and str(user_agent)[:_MAX_AGENT],
        detail=(detail or None) and str(detail)[:_MAX_DETAIL],
    )
    try:
        db.add(row)
        # Commit here rather than leaving it on the session: the caller is
        # usually about to return a response, and a rolled-back request must not
        # silently discard the record of a failed sign-in.
        db.commit()
        return row
    except Exception:  # pragma: no cover - depends on the database failing
        logger.warning("Could not write auth_event %s", event_type, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None