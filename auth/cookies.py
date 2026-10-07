"""
auth/cookies.py

Cookie transport for the session token, plus CSRF protection.

Why cookies at all
-----------------
The token used to live in localStorage, which any JavaScript on the page can read.
That means a single XSS bug hands an attacker a working session token. An
HttpOnly cookie is invisible to script, so the same bug cannot exfiltrate it --
the browser attaches it to requests without ever exposing the value.

The cost of cookies is CSRF: the browser attaches them to cross-site requests
automatically, which bearer tokens do not do. That is what the token half of this
module pays back.

Both are supported, deliberately
-------------------------------
A bearer token in the Authorization header still works, and is still what the
test suite, curl and any scripted integration use. The CSRF check therefore only
applies to requests that were authenticated *by cookie* -- the standard rule,
because a bearer token is never sent ambiently, so it cannot be ridden by a
third-party page in the first place.
"""

import hmac
import os
import secrets

from fastapi import HTTPException, Request, status

SESSION_COOKIE = "variantlens_session"
CSRF_COOKIE = "variantlens_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Methods that do not change state and therefore need no CSRF token.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

_COOKIE_MAX_AGE = 60 * 60  # matches JWT_EXPIRE_MINUTES


def _secure_flag() -> bool:
    """
    Whether to mark cookies Secure. Secure by default.

    A Secure cookie is silently dropped by the browser over plain HTTP, so
    running on http://localhost needs an explicit COOKIE_SECURE=false. That is
    the only reason this is configurable at all -- production should leave it
    alone.
    """
    return os.getenv("COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _samesite() -> str:
    # Lax still blocks cross-site POSTs (the CSRF vector that matters) while
    # allowing a bookmarked deep link to arrive with the session attached.
    return os.getenv("COOKIE_SAMESITE", "lax")


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookies(response, token: str, csrf_token: str | None = None) -> str:
    """
    Attach the session cookie and a readable CSRF cookie. Returns the CSRF token
    so the caller can also expose it to the client.
    """
    csrf = csrf_token or issue_csrf_token()
    kwargs = dict(
        httponly=False,           # must be readable: it is the double-submit half
        samesite=_samesite(),
        secure=_secure_flag(),
        path="/",
        max_age=_COOKIE_MAX_AGE,
    )
    response.set_cookie(CSRF_COOKIE, csrf, **kwargs)
    response.set_cookie(
        SESSION_COOKIE, token,
        **{**kwargs, "httponly": True},   # the whole point: not readable by script
    )
    return csrf


def clear_session_cookies(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def token_from_request(request: Request) -> tuple[str | None, bool]:
    """
    Return (token, authenticated_by_cookie).

    The Authorization header wins when both are present, so an explicit bearer
    token can override a stale cookie without needing to clear it first.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip(), False
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie, True
    return None, False


def require_csrf(request: Request, by_cookie: bool) -> None:
    """
    Enforce the double-submit check on state-changing cookie-authenticated calls.

    The client reads the CSRF cookie (which it is allowed to read) and echoes it
    in a header. A cross-origin page can cause the cookie to be sent but cannot
    read it, so it cannot produce a matching header.
    """
    if not by_cookie:
        return                      # bearer tokens are not ambient
    if request.method in SAFE_METHODS:
        return
    cookie_value = request.cookies.get(CSRF_COOKIE)
    header_value = request.headers.get(CSRF_HEADER)
    if not cookie_value or not header_value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing CSRF token. Send the "
                   f"{CSRF_HEADER} header when using cookie authentication.",
        )
    # Constant-time compare: a timing oracle here would let an attacker learn the
    # token a byte at a time.
    if not hmac.compare_digest(cookie_value, header_value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token does not match.",
        )