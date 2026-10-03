"""
auth/routes.py

Account endpoints. All are sync def handlers: the database layer is
synchronous, so FastAPI runs these in a threadpool instead of blocking
the event loop.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user
from auth.schemas import (
    LoginRequest,
    MeResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PasswordResetRequestedResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from auth.security import (
    JWT_EXPIRE_MINUTES,
    create_access_token,
    hash_password,
    verify_password,
)
from db.database import get_db
from db.models import Plan, User
from services.entitlement_service import entitlements, usage_this_month
from services.mailer import get_mailer, reset_code_body
from services.password_reset import CODE_TTL_MINUTES, issue_code, redeem_code

router = APIRouter(prefix="/api/auth", tags=["auth"])

# One response body for every outcome of a reset request, so the endpoint cannot
# be used to discover which email addresses have accounts.
_RESET_REQUEST_MESSAGE = (
    "If that email address has a VariantLens account, a verification code has "
    "been sent to it."
)


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
    """
    Exchange credentials for an access token.

    The identifier may be either the username or the email address. An "@" is
    not used to guess which -- both columns are matched in one query, so a
    username that happens to look like an email still works.
    """
    identifier = body.username.strip()
    # Emails are stored lower-cased; a user pasting "Alice@X.com" must still hit.
    candidate = identifier.lower() if "@" in identifier else identifier
    user = db.scalars(
        select(User).where(
            or_(User.username == identifier, User.email == candidate)
        )
    ).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # Identical response for "no such account" and "wrong password", so the
        # endpoint cannot be used to enumerate usernames or addresses.
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


@router.post("/password-reset/request",
             response_model=PasswordResetRequestedResponse)
def request_password_reset(body: PasswordResetRequest, db: Session = Depends(get_db)):
    """
    Issue a reset code to an email address.

    Always answers 200 with the same body whether or not the address exists --
    otherwise this endpoint would confirm which addresses are registered.

    dev_code is populated only by the log transport. With a real transport it is
    always null, so a production deployment cannot leak a code through the API.
    """
    mailer = get_mailer()
    email = str(body.email).strip().lower()

    user = db.scalars(select(User).where(User.email == email)).one_or_none()
    code = None
    if user is not None and user.is_active:
        # Deactivated accounts are skipped deliberately: the response is
        # identical, so this reveals nothing.
        _, code = issue_code(db, user)
        mailer.send(
            to_email=user.email,
            subject="Your VariantLens verification code",
            body=reset_code_body(code, CODE_TTL_MINUTES),
            secret=code,
        )

    return PasswordResetRequestedResponse(
        message=_RESET_REQUEST_MESSAGE,
        delivery=mailer.describe(),
        # Only the log transport may echo the code; a real transport never does.
        dev_code=code if (code and mailer.echo_secret) else None,
    )


@router.post("/password-reset/confirm", response_model=MeResponse)
def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    db: Session = Depends(get_db),
):
    """
    Redeem a reset code and set a new password.

    On success the caller is signed in immediately, which is what the UI does
    after the "set new password" step. Note this does NOT revoke tokens already
    issued: a reset replaces the credential, not every existing session.
    """
    email = str(body.email).strip().lower()
    user = db.scalars(select(User).where(User.email == email)).one_or_none()
    if user is None or not user.is_active:
        # Same message as a bad code, for the same enumeration reason.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That verification code is not valid or has expired.")

    ok, message = redeem_code(db, user, body.code)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    # redeem_code() already consumed the row, so it cannot be replayed.
    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)

    used = usage_this_month(db, user.id)
    info = entitlements(user, used)
    token = create_access_token(user.id, user.username, user.role)
    return MeResponse(
        user=_public_user(user),
        plan=user.plan,
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
        access_token=token,
        expires_in=JWT_EXPIRE_MINUTES * 60,
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
