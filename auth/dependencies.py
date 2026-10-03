"""
auth/dependencies.py

FastAPI dependencies that turn a Bearer token into an authenticated User row.

Re-reading the row on every request (instead of trusting token contents) means a
deactivated account loses access immediately, even with an unexpired token.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.security import TokenError, decode_access_token
from db.database import get_db
from db.models import User

_bearer = HTTPBearer(auto_error=False)


def bearer_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    if credentials is None or not credentials.credentials:
        detail = "Missing Authorization: Bearer <token> header."
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    return credentials.credentials


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
