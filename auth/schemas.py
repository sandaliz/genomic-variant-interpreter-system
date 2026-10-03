"""
auth/schemas.py

Pydantic v2 request/response contracts for the account layer.
"""

from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    # Accepts a username OR an email address; the route resolves both.
    username: str = Field(min_length=1, max_length=255,
                          description="Username or email address")
    password: str = Field(min_length=1, max_length=128)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=8, max_length=128)


class PasswordResetRequestedResponse(BaseModel):
    """
    Always the same shape whether or not the address exists.

    `delivery` names the transport used, and `dev_code` carries the code ONLY
    when the log transport is active -- so a misconfigured production instance
    cannot leak codes through the response body.
    """

    message: str
    delivery: str
    dev_code: Optional[str] = None


class UserOut(BaseModel):
    """Public account shape. The password hash is never included."""

    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    role: str
    plan_id: int
    is_active: bool

    model_config = {"from_attributes": True}


class PlanOut(BaseModel):
    id: int
    name: str
    price_cents: Optional[int] = None
    monthly_query_limit: Optional[int] = None
    llm_enabled: bool
    api_access: bool
    audit_export: bool

    model_config = {"from_attributes": True}


class UsageSummary(BaseModel):
    used_this_month: int
    monthly_limit: Optional[int] = None
    remaining: Optional[int] = None


class EntitlementsOut(BaseModel):
    llm_enabled: bool
    api_access: bool
    audit_export: bool


class MeResponse(BaseModel):
    user: UserOut
    plan: PlanOut
    usage: UsageSummary
    entitlements: EntitlementsOut
    # Present only when the endpoint also mints a session (password reset),
    # so the UI can sign the user in without a second round-trip.
    access_token: Optional[str] = None
    expires_in: Optional[int] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ------------------------------------------------------------------
# Upgrade requests
# ------------------------------------------------------------------
class UpgradeRequestCreate(BaseModel):
    target_plan_id: int
    note: Optional[str] = Field(default=None, max_length=500)


class UpgradeRequestOut(BaseModel):
    """A request as the requesting user sees it."""

    id: int
    target_plan: str
    target_plan_id: int
    note: Optional[str] = None
    status: str
    admin_note: Optional[str] = None
    created_at: Optional[str] = None
    decided_at: Optional[str] = None


class AdminUpgradeRequestOut(UpgradeRequestOut):
    """
    A request as an administrator sees it.

    Carries the requester's identity because the hand-off is a conversation:
    the admin needs to know who to reply to, not just what was asked for.
    """

    user_id: int
    username: str
    email: str
    current_plan: str


class UpgradeDecision(BaseModel):
    """Administrator action on a pending request."""

    status: str = Field(description="approved, declined or contacted")
    admin_note: Optional[str] = Field(default=None, max_length=500)
