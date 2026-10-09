"""
scripts/seed_plans.py

Create (or update) the three subscription plans described in the report.

Idempotent: safe to run repeatedly. Existing rows are updated in place, so this
file stays the single source of truth for plan limits.

    python scripts/seed_plans.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from db.database import SessionLocal
from db.models import Plan

# Illustrative academic pricing -- NOT a shipping offer. Mirrors the pricing
# table in PROJECT_REPORT.md section 10.
PLANS = [
    {
        "name": "Free",
        "price_cents": 0,
        "monthly_query_limit": 10,
        "llm_enabled": False,
        "api_access": False,
        "audit_export": False,
    },
    {
        "name": "Professional",
        "price_cents": 9900,           # $99.00 / month
        "monthly_query_limit": 500,
        "llm_enabled": True,
        "api_access": True,
        "audit_export": True,
    },
    {
        "name": "Enterprise",
        "price_cents": None,           # custom pricing
        "monthly_query_limit": None,   # None means unlimited
        "llm_enabled": True,
        "api_access": True,
        "audit_export": True,
    },
]


def main() -> int:
    db = SessionLocal()
    try:
        created = updated = 0
        for spec in PLANS:
            plan = db.scalars(select(Plan).where(Plan.name == spec["name"])).one_or_none()
            if plan is None:
                db.add(Plan(**spec))
                created += 1
            else:
                for key, value in spec.items():
                    setattr(plan, key, value)
                updated += 1
        db.commit()

        print(f"plans seeded: {created} created, {updated} updated")
        for plan in db.scalars(select(Plan).order_by(Plan.id)).all():
            limit = "unlimited" if plan.is_unlimited else plan.monthly_query_limit
            price = "custom" if plan.price_cents is None else f"${plan.price_cents / 100:.2f}"
            print(f"  {plan.id}: {plan.name:<13} {price:<8} limit={limit} "
                  f"llm={plan.llm_enabled} api={plan.api_access} audit={plan.audit_export}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
