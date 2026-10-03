"""
auth/routes.py

Account endpoints. All are sync def handlers: the database layer is
synchronous, so FastAPI runs these in a threadpool instead of blocking
the event loop.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user
from auth.schemas import LoginRequest, MeResponse, RegisterRequest, TokenResponse, UserOut
from auth.security import JWT_EXPIRE_MINUTES, create_access_token, hash_password, verify_password
from db.database import get_db
from db.models import Plan, User
from services.entitlement_service import entitlements, usage_this_month

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _public_user(user: User) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserOut)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    username = body.username.strip()
    email = str(body.email).strip().lower()
    if db.scalars(select(User).where(User.username == username)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Username is already registered.")
    if db.scalars(select(User).where(User.email == email)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Email address is already registered.")
    plan = db.scalars(select(Plan).where(Plan.name == "Free")).one_or_none()
    if plan is None:
        detail = "Free plan is missing. Run scripts/seed_plans.py first."
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=detail)
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(body.password),
        role="clinician",  # self-registration can never create an admin
        plan_id=plan.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _public_user(user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    username = body.username.strip()
    user = db.scalars(select(User).where(User.username == username)).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Account is deactivated.")
    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=token,
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=_public_user(user),
    )


@router.get("/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    used = usage_this_month(db, current_user.id)
    info = entitlements(current_user, used)
    return MeResponse(
        user=_public_user(current_user),
        plan=current_user.plan,
        usage={
            "used_this_month": used,
            "monthly_limit": info["monthly_limit"],
            "remaining": info["remaining"],
        },
        entitlements={
            "llm_enabled": info["llm_enabled"],
            "api_access": info["api_access"],
            "audit_export": info["audit_export"],
        },
    )
