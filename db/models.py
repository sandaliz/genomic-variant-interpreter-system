"""
db/models.py

ORM models for the account/entitlement layer.

Design notes
------------
* These tables sit around the four-agent pipeline. The agents never read or
  write them, so the agentic core remains unchanged.
* Role and plan are deliberately independent: an account can be an admin on the
  Free plan, or a clinician on Enterprise.
* monthly_query_limit is nullable -- NULL means unlimited (Enterprise).
* Columns use generic SQLAlchemy types, so the test suite can run against SQLite
  when PostgreSQL is not available.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base

# The two roles understood by the application. Kept as a plain tuple so the auth
# layer validates against the same source of truth.
ALLOWED_ROLES = ("admin", "clinician")


def _utcnow() -> datetime:
    """Timezone-aware UTC now -- the project uses UTC everywhere for usage."""
    return datetime.now(timezone.utc)


class Plan(Base):
    """A subscription tier; mirrors the three illustrative plans in the report."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Price in whole cents; NULL means custom pricing / contact sales.
    price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # NULL means unlimited queries per month.
    monthly_query_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    audit_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    users: Mapped[list["User"]] = relationship(back_populates="plan")

    @property
    def is_unlimited(self) -> bool:
        return self.monthly_query_limit is None

    def __repr__(self) -> str:
        return f"<Plan {self.name} limit={self.monthly_query_limit}>"


class User(Base):
    """An authenticated account. Belongs to exactly one plan."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # native_enum=False -> VARCHAR + CHECK constraint, portable across
    # PostgreSQL (production and integration tests) and SQLite (fast unit tests).
    role: Mapped[str] = mapped_column(
        SAEnum(*ALLOWED_ROLES, name="user_role", native_enum=False, validate_strings=True),
        nullable=False,
        default="clinician",
    )
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # joined: evaluating entitlements always needs the plan, so load it in the
    # same query instead of issuing a lazy load on every request.
    plan: Mapped[Plan] = relationship(back_populates="users", lazy="joined")
    usage_records: Mapped[list["UsageRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.username} role={self.role} plan_id={self.plan_id}>"


class UsageRecord(Base):
    """One row per accepted /api/classify call, used for monthly quota checks."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Unique request id makes recording idempotent: a client retry can never be
    # counted twice against the monthly quota.
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    llm_tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="usage_records")

    def __repr__(self) -> str:
        return f"<UsageRecord user={self.user_id} at={self.created_at}>"
