"""
scripts/create_admin.py

Create (or reset) the first administrator account.

The password is never hard-coded: pass it through the ADMIN_PASSWORD environment
variable, or let the script prompt for it with hidden input. Running the script
again with the same username updates that account instead of failing.

    python scripts/create_admin.py

    ADMIN_USERNAME=admin ADMIN_EMAIL=admin@example.com \
        ADMIN_PASSWORD=... python scripts/create_admin.py

Note on the default email: it must be an address the *application* considers
valid. Everything that touches an email goes through Pydantic's EmailStr, which
rejects special-use and reserved domains (.local, .test, .localhost, .invalid).
Seeding "admin@variantlens.local" therefore produced an account that could never
use password recovery, because the address it was registered against was
rejected as malformed. The email is validated below so that failure is loud
rather than silent.
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from auth.security import hash_password
from db.database import SessionLocal
from db.models import Plan, User

DEFAULT_USERNAME = "admin"
DEFAULT_EMAIL = "admin@example.com"
MIN_PASSWORD_LENGTH = 8

# Same validator the API uses, so the bootstrap can never disagree with it.
EMAIL_ADAPTER = TypeAdapter(str)
try:  # Pydantic v2 ships EmailStr; fall back cleanly if unavailable.
    from pydantic import EmailStr as _EmailStr

    EMAIL_ADAPTER = TypeAdapter(_EmailStr)
except ImportError:  # pragma: no cover - EmailStr is a hard dependency
    pass


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

    # Validate before touching the database. Writing an address the API would
    # later reject is how the previous default locked the admin out of recovery.
    try:
        EMAIL_ADAPTER.validate_python(email)
    except ValidationError:
        raise SystemExit(
            f"Refusing to create the account: {email!r} is not a valid email "
            f"address (the API rejects it too). Pass ADMIN_EMAIL=... with an "
            f"address on a real domain."
        )

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
