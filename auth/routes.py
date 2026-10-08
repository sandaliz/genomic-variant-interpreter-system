"""
auth/routes.py

Account endpoints. All are sync def handlers: the database layer is
synchronous, so FastAPI runs these in a threadpool instead of blocking
the event loop.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.audit import record
from auth.cookies import clear_session_cookies, set_session_cookies
from auth.dependencies import get_current_user
from auth.rate_limit import (
    RateLimitExceeded,
    _client_ip,
    check_login_allowed,
    reset_login_attempts,
)
from auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PasswordResetRequestedResponse,
    ProfileUpdateRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from auth.security import (
    JWT_EXPIRE_MINUTES,
    create_access_token,
    hash_password,
    verify_password,
    verify_password_or_dummy,
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
    record(db, "register", user_id=user.id, identifier=email)
    return _public_user(user)


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Exchange credentials for an access token.

    Email is the only accepted identifier. Username remains a display handle --
    unique and immutable, but never a credential -- so signing in cannot be
    confused with claiming a name. Addresses are stored lower-cased, and the
    lookup is lower-cased to match, because email is case-insensitive in
    practice and "Alice@X.com" must reach the same account as "alice@x.com".

    Throttled per client address and per identifier, and every outcome is written
    to the audit trail.
    """
    identifier = str(body.email).strip().lower()

    # Checked before the password is verified, so a blocked caller costs no
    # Argon2id work.
    try:
        check_login_allowed(request, identifier)
    except RateLimitExceeded as exc:
        record(
            db, "login_rate_limited", succeeded=False, identifier=identifier,
            client_ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
            detail=f"retry_after={exc.retry_after}",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again in a few minutes.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    # Emails are stored lower-cased (register and profile-update both do it), so
    # the lookup lower-cases too. A user pasting "Alice@X.com" still signs in.
    user = db.scalars(select(User).where(User.email == identifier)).one_or_none()
    # verify_password_or_dummy burns the same Argon2id work for "no such
    # account" as for "wrong password", so response time cannot reveal which
    # email addresses have accounts (the message was already identical).
    if not verify_password_or_dummy(
        body.password, user.password_hash if user is not None else None
    ):
        # Identical response for "no such account" and "wrong password", so the
        # endpoint cannot be used to enumerate addresses. The audit record still
        # distinguishes them, which is the point of having one.
        record(
            db, "login_failed", succeeded=False, identifier=identifier,
            user_id=user.id if user is not None else None,
            client_ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
            detail="no match" if user is None else "bad password",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid email or password.")
    if not user.is_active:
        record(
            db, "login_failed", succeeded=False, user_id=user.id,
            identifier=identifier, client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            detail="account deactivated",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Account is deactivated.")

    # A correct password clears the throttle, so a user who mistyped a couple of
    # times is not left locked out of their own account.
    reset_login_attempts(identifier)
    record(
        db, "login_success", user_id=user.id, identifier=identifier,
        client_ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    token = create_access_token(user.id, user.username, user.role)
    # Two delivery mechanisms on purpose. The cookie is what the browser uses --
    # HttpOnly, so script cannot read it. access_token in the body stays for curl,
    # the test suite and scripted integrations.
    csrf = set_session_cookies(response, token)
    return TokenResponse(
        access_token=token,
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=_public_user(user),
        csrf_token=csrf,
    )


@router.post("/logout")
def logout(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Clear the session cookies.

    The token itself cannot be revoked without either a deny-list or rotating the
    signing key, so signing out here means "this browser no longer presents a
    credential". Anything already holding the token keeps its access until it
    expires -- which is also why clearing the client-side copy matters.
    """
    clear_session_cookies(response)
    record(db, "logout", user_id=current_user.id, actor_user_id=current_user.id)
    return {"signed_out": True}


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

    # Audited on both paths. The identifier is hashed, so recording a request
    # against an address that belongs to nobody is not the same as storing it.
    record(
        db, "password_reset_requested",
        user_id=user.id if user is not None else None,
        identifier=email,
        detail="code issued" if code else "no matching active account",
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
    response: Response,
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
    record(db, "password_reset_completed", user_id=user.id, identifier=email)

    used = usage_this_month(db, user.id)
    info = entitlements(user, used)
    token = create_access_token(user.id, user.username, user.role)
    csrf = set_session_cookies(response, token)
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
        csrf_token=csrf,
    )


def _me_response(user: User, db: Session) -> MeResponse:
    """Build the standard /me payload for an account."""
    used = usage_this_month(db, user.id)
    info = entitlements(user, used)
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
    )


@router.get("/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    The signed-in account, its plan, its monthly usage and its entitlements.

    This is what the profile page and the header quota meter are built from.
    """
    return _me_response(current_user, db)


@router.patch("/me", response_model=MeResponse)
def update_profile(
    body: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update the signed-in account's own details.

    full_name is free to change. email requires the current password, because
    the address is where password recovery goes: without that check a stolen
    session could redirect recovery to an attacker-controlled inbox and take the
    account over permanently. username cannot be changed here.
    """
    if body.email is not None:
        new_email = str(body.email).strip().lower()
        if new_email != current_user.email:
            if not body.current_password or not verify_password(
                body.current_password, current_user.password_hash
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Current password is required to change your email address.")
            clash = db.scalars(select(User).where(User.email == new_email)).first()
            if clash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That email address is already registered.")
            current_user.email = new_email

    if body.full_name is not None:
        cleaned = body.full_name.strip()
        current_user.full_name = cleaned or None

    db.commit()
    db.refresh(current_user)
    return _me_response(current_user, db)


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Change the signed-in account's password by proving the current one.

    This is the profile-page path, and it is deliberately different from the
    forgot-password flow: that one needs a mailed code because the credential is
    gone, this one proves ownership with the credential still in hand. It also
    does not revoke tokens already issued -- a password change replaces the
    credential, not every live session.
    """
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect.")
    if verify_password(body.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="New password must be different from the current one.")

    current_user.password_hash = hash_password(body.new_password)
    db.commit()
    record(db, "password_changed", user_id=current_user.id,
           actor_user_id=current_user.id)
    # Response says only that it happened -- never the password, never the hash.
    return {"changed": True}
