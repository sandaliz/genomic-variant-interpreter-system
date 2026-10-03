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

import datetime
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
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

# Lifecycle of an upgrade request. `contacted` exists as its own state because
# "the admin reached out" is a real, reportable step that happens before a
# decision -- collapsing it into pending would hide that from the user and the
# audit trail.
UPGRADE_STATUSES = ("pending", "contacted", "approved", "declined")
# Only these may ever be transitioned into; approved/declined are terminal.
DECIDABLE_STATUSES = ("approved", "declined")


def _utcnow() -> datetime:
    """Timezone-aware UTC now -- the project uses UTC everywhere for usage."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """
    Normalise a stored datetime to aware UTC.

    PostgreSQL returns aware timestamps for DateTime(timezone=True) and SQLite
    returns naive ones, so comparisons must not assume either dialect.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    # Optional human name for the profile page. NULL means the account never set
    # one, and the UI falls back to the username -- a real name is never required.
    full_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
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
    # No cascade: deleting an account must never delete a sign-off. The FK is
    # ON DELETE SET NULL, so the attestation survives with its reviewer name.
    review_signoffs: Mapped[list["ReviewSignoff"]] = relationship(
        back_populates="reviewer", passive_deletes=True
    )
    # Security records outlive the password they protect: deleting an account
    # must not erase the history of reset codes issued against it.
    password_resets: Mapped[list["PasswordReset"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # Unlike sign-offs, these are the account's own administrative rows, so
    # they cascade with the account rather than surviving it.
    upgrade_requests: Mapped[list["UpgradeRequest"]] = relationship(
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
class ReviewSignoff(Base):
    """
    A genetic counselor's sign-off on one interpreted variant.

    This record is the accountable-attestation half of the product: the system
    produces a machine classification, and a human signs off on it. It was a
    local JSON file before (data/review_signoffs.json, gitignored) which meant
    it had no ownership, no audit trail and no backup.

    Design notes:
    * One sign-off per variant (unique variant_id) — this matches the existing
      "pending / reviewed" UI state. Re-signing updates the row in place rather
      than appending, so the current state is always a single row.
    * reviewer_name is the human who signs (may not be the account holder, e.g.
      a counselor signing off on behalf of a clinician), while reviewed_by is
      the authenticated account that recorded it.
    * ondelete="SET NULL": if the account is deleted the sign-off survives with
      its reviewer name and date intact. Deleting an account must not be able
      to erase a clinical attestation.
    """

    __tablename__ = "review_signoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    reviewer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    review_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    signature: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    reviewed_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    reviewer: Mapped[Optional["User"]] = relationship(back_populates="review_signoffs")

    def __repr__(self) -> str:
        return f"<ReviewSignoff {self.variant_id} by {self.reviewer_name}>"


class PasswordReset(Base):
    """
    A one-time password-reset code issued to an account.

    Design notes:
    * The code is NEVER stored in plaintext -- only a salted SHA-256 digest, so
      a database leak does not hand out working reset codes.
    * Single use: consumed_at is stamped on success, and a consumed or expired
      row can never be replayed.
    * attempts is capped, so a leaked 6-digit code cannot be brute-forced
      through the confirm endpoint (there are only a million possibilities).
    * Rows are kept after use rather than deleted, so "was a reset attempted on
      this account?" stays answerable for the audit trail.
    """

    __tablename__ = "password_resets"
    __table_args__ = (
        # Lookups always filter by user and then by recency.
        Index("ix_password_resets_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Kept for the audit trail even if the account's email later changes.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="password_resets")

    @property
    def is_usable(self) -> bool:
        """True while this code can still be redeemed."""
        return self.consumed_at is None and _as_utc(self.expires_at) > _utcnow()
class UpgradeRequest(Base):
    """
    A request from an account to move to a paid plan.

    There is no payment integration, and that is deliberate: this models the
    *hand-off* -- the user asks, an administrator reviews and decides, and the
    decision is recorded. That is the part worth demonstrating, and pretending
    to take money would be the dishonest part.

    Lifecycle
    ---------
        pending --> contacted --> approved   (applies the plan change)
                   \-> declined

    approved and declined are terminal: a decided request cannot be quietly
    re-opened, which keeps the history readable.
    """

    __tablename__ = "upgrade_requests"
    __table_args__ = (
        Index("ix_upgrade_user_created", "user_id", "created_at"),
        # The admin queue is always "newest first, grouped by status".
        Index("ix_upgrade_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        SAEnum(*UPGRADE_STATUSES, name="upgrade_status", native_enum=False,
               validate_strings=True),
        nullable=False,
        default="pending",
    )
    admin_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship()
    target_plan: Mapped[Plan] = relationship(foreign_keys=[target_plan_id])

    @property
    def is_open(self) -> bool:
        """True while the request is still awaiting a decision."""
        return self.status in ("pending", "contacted")

    def __repr__(self) -> str:
        return f"<UpgradeRequest user={self.user_id} plan={self.target_plan_id} {self.status}>"

    def __repr__(self) -> str:
        state = "consumed" if self.consumed_at else "live"
        return f"<PasswordReset user={self.user_id} {state}>"
