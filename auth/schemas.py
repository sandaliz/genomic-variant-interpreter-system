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
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    """Public account shape. The password hash is never included."""

    id: int
    username: str
    email: str
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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
