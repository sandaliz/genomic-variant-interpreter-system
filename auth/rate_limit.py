"""
auth/rate_limit.py

A sliding-window rate limiter for credential endpoints.

Argon2id is deliberately slow, which raises the cost of each guess but does not
cap how many guesses may be made. Without a limit, an unbounded number of password
attempts can be issued against a single account, so this is the control that
actually closes brute force.

Two independent counters are kept per caller:

  * by client IP, which bounds total guesses arriving from one host, and
  * by identifier, which bounds guesses aimed at one account however many hosts
    they come from.

Both are needed. Limiting on IP alone lets a botnet walk a single account; limiting
on identifier alone lets one attacker lock every user out by cycling usernames.
The identifier counter is the one that matters for account safety, so the lockout
is reported as "too many attempts" rather than exposing which half tripped.

Storage is in-process and therefore per-worker: correct for the single-process
uvicorn this project runs. A multi-worker or multi-host deployment would need a
shared store (Redis) instead, or the effective limit becomes the per-worker limit
multiplied by the worker count. That caveat is repeated at the definition.
"""

import threading
import time
from collections import deque

# Per-worker note: see the module docstring.
_LOCK = threading.Lock()

# window_seconds, max_attempts
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5

# Buckets, keyed by ("ip", value) or ("account", value).
_BUCKETS: dict = {}


class RateLimitExceeded(Exception):
    """Raised when a caller has used up its allowance in the current window."""

    def __init__(self, retry_after: int):
        super().__init__("Too many attempts. Try again shortly.")
        self.retry_after = retry_after


def _prune(bucket: deque, window: int, now: float) -> None:
    """Drop timestamps that have fallen out of the window."""
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def _check(key, now: float) -> int | None:
    """
    Record an attempt for `key` and return the seconds to wait if it is blocked.

    Returns None when the attempt is allowed.
    """
    kind, value = key
    window = DEFAULT_WINDOW_SECONDS
    bucket_key = (kind, value, window)
    bucket = _BUCKETS.setdefault(bucket_key, deque())
    limit = _limit_for(kind)

    with _LOCK:
        _prune(bucket, window, now)
        if len(bucket) >= limit:
            # Seconds until the oldest attempt leaves the window.
            return max(1, int(window - (now - bucket[0])) + 1)
        bucket.append(now)
    return None


def _limit_for(kind: str) -> int:
    if kind == "account":
        return DEFAULT_MAX_ATTEMPTS
    # The IP limit is looser: a clinic shares an egress address, so a tight limit
    # here would punish ordinary shared-NAT users without stopping a patient
    # targeting one account.
    return DEFAULT_MAX_ATTEMPTS * 4


def _client_ip(request) -> str:
    """
    Best-effort client address.

    X-Forwarded-For is only consulted when the app is explicitly behind a proxy
    that sets FRONTEND_ORIGINS/FRONTEND_TRUST_PROXY. Trusting it unconditionally
    would let any caller spoof their address and reset the limit on every request.
    """
    import os

    if os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login_allowed(request, identifier: str) -> None:
    """
    Raise RateLimitExceeded if this caller has exhausted its allowance.

    Checked *before* the password is verified, so a blocked caller costs no
    Argon2id work.
    """
    now = time.monotonic()
    ip_retry = _check(("ip", _client_ip(request)), now)
    account_retry = _check(("account", (identifier or "").strip().lower()), now)
    if account_retry is not None:
        raise RateLimitExceeded(account_retry)
    if ip_retry is not None:
        raise RateLimitExceeded(ip_retry)


def reset_login_attempts(identifier: str) -> None:
    """
    Clear the counters after a successful sign-in.

    Without this a legitimate user who mistyped their password a few times would
    stay throttled even after they got it right.
    """
    value = (identifier or "").strip().lower()
    with _LOCK:
        for key in [k for k in _BUCKETS if k[1] == value]:
            _BUCKETS.pop(key, None)


def reset_all() -> None:
    """Clear every bucket. Used by the test suite between tests."""
    with _LOCK:
        _BUCKETS.clear()