"""
scripts/create_admin.py

Create (or reset) the first administrator account.

The password is never hard-coded: pass it through the ADMIN_PASSWORD environment
variable, or let the script prompt for it with hidden input. Running the script
again with the same username updates that account instead of failing.

    python scripts/create_admin.py

    ADMIN_USERNAME=admin ADMIN_EMAIL=admin@example.com \
        ADMIN_PASSWORD=... python scripts/create_admin.py
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from auth.security import hash_password
from db.database import SessionLocal
from db.models import Plan, User

DEFAULT_USERNAME = "admin"
DEFAULT_EMAIL = "admin@variantlens.local"
MIN_PASSWORD_LENGTH = 8


def _resolve_password() -> str:
    password = os.getenv("ADMIN_PASSWORD")
    if password:
        return password
    first = getpass.getpass("Admin password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    return first


def main() -> int:
    username = os.getenv("ADMIN_USERNAME", DEFAULT_USERNAME)
    email = os.getenv("ADMIN_EMAIL", DEFAULT_EMAIL)
    password = _resolve_password()

    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    db = SessionLocal()
    try:
        plan = db.scalars(select(Plan).where(Plan.name == "Enterprise")).one_or_none()
        if plan is None:
            raise SystemExit("Enterprise plan missing -- run scripts/seed_plans.py first.")

        user = db.scalars(select(User).where(User.username == username)).one_or_none()
        action = "updated" if user is not None else "created"
        if user is None:
            user = User(username=username, email=email)
            db.add(user)

        user.email = email
        user.password_hash = hash_password(password)
        user.role = "admin"
        user.plan_id = plan.id
        user.is_active = True
        db.commit()

        print(f"admin {action}: {user.username} <{user.email}> "
              f"role={user.role} plan={plan.name}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
