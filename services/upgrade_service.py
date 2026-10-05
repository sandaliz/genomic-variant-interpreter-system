"""
services/upgrade_service.py

The upgrade hand-off: a user asks for a paid plan, an administrator reviews and
decides.

There is no payment processing here, on purpose. What is modelled is the part
that is actually interesting and honest -- who asked for what, who decided, and
when. Silently flipping a plan on a self-serve request would make the
commercialization story look finished when it is not.

Rules enforced here rather than in the routes, so they hold no matter which
endpoint is called:
  * only paid plans are requestable (Free is not an upgrade);
  * you cannot request the plan you are already on;
  * one open request per account at a time;
  * approved/declined are terminal;
  * approving applies the plan change in the same transaction as the decision.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import DECIDABLE_STATUSES, Plan, UpgradeRequest, User


class UpgradeError(HTTPException):
    """An upgrade request that cannot proceed as asked."""

    def __init__(self, detail: str, status_code: int = status.HTTP_409_CONFLICT):
        super().__init__(status_code=status_code, detail=detail)


def open_request_for(db: Session, user: User) -> Optional[UpgradeRequest]:
    """This account's currently-open request, if any."""
    return db.scalars(
        select(UpgradeRequest).where(
            UpgradeRequest.user_id == user.id,
            UpgradeRequest.status.in_(["pending", "contacted"]),
        )
        .order_by(UpgradeRequest.created_at.desc())
        .limit(1)
    ).one_or_none()


def create_request(db: Session, user: User, target_plan_id: int,
                   note: Optional[str] = None) -> UpgradeRequest:
    """Create an upgrade request after validating the request is sensible."""
    plan = db.get(Plan, target_plan_id)
    if plan is None:
        raise UpgradeError("Unknown plan.", status.HTTP_404_NOT_FOUND)

    # Free is the starting tier, not something to upgrade to.
    if plan.price_cents == 0:
        raise UpgradeError("Free is the default plan; choose a paid plan to upgrade.")

    if user.plan_id == plan.id:
        raise UpgradeError(f"Your account is already on {plan.name}.")

    if open_request_for(db, user) is not None:
        raise UpgradeError(
            "You already have an upgrade request awaiting a decision. "
            "Wait for a response before requesting another."
        )

    request = UpgradeRequest(
        user_id=user.id,
        target_plan_id=plan.id,
        note=(note or "").strip() or None,
        status="pending",
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def decide(db: Session, request: UpgradeRequest, decision: str,
           admin_note: Optional[str] = None) -> UpgradeRequest:
    """
    Record an administrator's decision and, on approval, move the account.

    The status change and the plan change share one transaction so an approval
    can never be recorded without the plan actually moving.
    """
    if decision not in DECIDABLE_STATUSES:
        raise UpgradeError(
            f"decision must be one of: {', '.join(DECIDABLE_STATUSES)}.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if not request.is_open:
        raise UpgradeError(
            f"This request was already {request.status} and cannot be changed."
        )

    request.status = decision
    request.admin_note = (admin_note or "").strip() or None
    request.updated_at = datetime.now(timezone.utc)

    if decision == "approved":
        user = db.get(User, request.user_id)
        # Guard: approving a request for the plan the account already holds
        # would be a no-op that looks like a successful upgrade.
        if user is not None and user.plan_id != request.target_plan_id:
            user.plan_id = request.target_plan_id
    request.decided_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(request)
    return request


def mark_contacted(db: Session, request: UpgradeRequest,
                   admin_note: Optional[str] = None) -> UpgradeRequest:
    """Record that an administrator reached out, without deciding yet."""
    if not request.is_open:
        raise UpgradeError(
            f"This request was already {request.status} and cannot be changed.")
    request.status = "contacted"
    if admin_note is not None and admin_note.strip():
        request.admin_note = admin_note.strip()
    request.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)
    return request


def decision_body(request: UpgradeRequest, plan_name: str, user_name: str) -> str:
    """Plain-text notice sent to the user when a request is decided."""
    outcome = ("has been approved" if request.status == "approved"
               else "has been declined")
    lines = [
        f"Your VarientLens upgrade request for the {plan_name} plan {outcome}.",
        "",
    ]
    if request.admin_note:
        lines += [f"    Note from the administrator: {request.admin_note}", ""]
    lines.append("Sign in to your profile page to see your current plan.")
    return "\n".join(lines)