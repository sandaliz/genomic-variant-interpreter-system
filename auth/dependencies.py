"""
auth/dependencies.py

FastAPI dependencies that turn a Bearer token or session cookie into an
authenticated User row.

Re-reading the row on every request (instead of trusting token contents) means a
deactivated account loses access immediately, even with an unexpired token.

A request may authenticate two ways:

  * ``Authorization: Bearer <token>`` -- used by the test suite, curl and any
    scripted integration. Not ambient, so no CSRF check applies.
  * the HttpOnly session cookie -- used by the browser, where a token in
    localStorage would be readable by any injected script. Ambient, so a CSRF
    token is required (see auth/cookies.py).
"""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.cookies import require_csrf, token_from_request
from auth.security import TokenError, decode_access_token
from db.database import get_db
from db.models import User


def bearer_token(request: Request) -> str:
    """
    Pull the token off the request, enforcing CSRF where it applies.
    """
    token, by_cookie = token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization: Bearer <token> header or session cookie.",
        )
    require_csrf(request, by_cookie)
    return token


def get_current_user(
    token: str = Depends(bearer_token), db: Session = Depends(get_db)
) -> User:
    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    try:
        user_id = int(claims.get("sub", ""))
    except (TypeError, ValueError):
        detail = "Token subject is not a valid user id."
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    user = db.scalars(select(User).where(User.id == user_id)).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Account no longer exists.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Account is deactivated.")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        detail = "Admin role required. Clinician accounts cannot reach this endpoint."
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return current_user
