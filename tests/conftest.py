"""
Pytest fixtures shared by every test module.

The account/entitlement layer is tested against an in-memory SQLite database so
the suite stays fast and needs no running PostgreSQL. The model definitions are
deliberately portable (native_enum=False, generic column types) precisely so
this is possible; the production path is PostgreSQL via DATABASE_URL.

That also means a contributor can run the whole suite with no .env file at all:
the placeholder DATABASE_URL below satisfies the db.database import, and no test
ever connects to it.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set before db.database is imported, which raises without it. The URL is never
# dialled: every test overrides get_db with its own SQLite engine.
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")

# auth.security refuses to import unless both JWT secrets are configured, which
# is the point in production. The suite needs its own keys rather than the
# developer's, so they are set here before the first import.
os.environ.setdefault("JWT_SECRET_KEY", "test-signing-key-not-a-real-secret")
os.environ.setdefault("JWT_ENCRYPTION_KEY", "test-encryption-key-not-a-real-secret")

# Cookies default to Secure, and a Secure cookie is dropped over plain HTTP --
# which is what the test client uses. Opt out for the suite only; production
# should never set this.
os.environ.setdefault("COOKIE_SECURE", "false")

# Imported for side effects: db.models must be registered on Base.metadata
# before create_all() runs, otherwise the tables would not exist.
from db.database import Base  # noqa: E402
from db.models import Plan, UsageRecord, User  # noqa: E402,F401


@pytest.fixture
def db_session():
    """
    A fresh in-memory database per test, torn down afterwards.

    StaticPool + check_same_thread=False are both required, not cosmetic:
    TestClient runs the app on a worker thread, and an in-memory SQLite
    database is per-connection -- without a single shared connection the
    request thread would see an empty database.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_plan(session, name="Free", limit=3, llm=False, api_access=True,
              audit_export=False, price_cents=0):
    """Insert a plan row for tests."""
    plan = Plan(
        name=name,
        price_cents=price_cents,
        monthly_query_limit=limit,
        llm_enabled=llm,
        api_access=api_access,
        audit_export=audit_export,
    )
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


def make_user(session, username="clinician", plan=None, role="clinician",
              password="correct-horse-battery", is_active=True, email=None):
    """
    Insert a user row for tests, defaulting to a fresh Free plan.

    The address defaults to "<username>@example.com", which is what almost every
    test wants. Pass email= explicitly when the handle is itself address-shaped
    and you need the two to differ -- the derived default would not be a valid
    address in that case.
    """
    from auth.security import hash_password

    if plan is None:
        plan = make_plan(session)
    user = User(
        username=username,
        email=email if email is not None else f"{username}@example.com",
        password_hash=hash_password(password),
        role=role,
        plan_id=plan.id,
        is_active=is_active,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def plan_named(session, name, **kwargs):
    """
    Fetch a plan by name, creating it only if absent.

    Several tests (and their fixtures) legitimately need the same plan -- e.g.
    two administrators both on Enterprise -- and plans.name is UNIQUE, so
    creating a second one would fail the test for the wrong reason.
    """
    existing = session.scalars(select(Plan).where(Plan.name == name)).one_or_none()
    if existing is not None:
        return existing
    return make_plan(session, name=name, **kwargs)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """
    The sign-in limiter is process-global by design, which means its counters
    survive between tests and a suite that logs in repeatedly with the same
    username would start failing with 429 for no real reason. Clearing the
    buckets between tests keeps each one independent.
    """
    from auth.rate_limit import reset_all

    reset_all()
    yield
    reset_all()


@pytest.fixture
def client(db_session):
    """
    FastAPI TestClient whose get_db dependency is redirected at the in-memory
    session, so the whole HTTP surface is exercised without PostgreSQL.
    """
    from api import app
    from db.database import get_db

    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def login(client, db_session):
    """Log in through the real endpoint and return the bearer header.

    Takes a *username* and sends the account's actual email address, because
    email is the only accepted login identifier. Resolving it from the row keeps
    every existing call site reading in terms of the handle they created.

    The cookie jar is cleared afterwards. Logging in now also sets a session
    cookie and a TestClient carries cookies forward, so without this every
    later request in the test would also be cookie-authenticated -- silently
    changing what "anonymous" means and triggering CSRF enforcement on tests
    that only ever pass the bearer header. The cookie path has its own tests.
    """

    def _login(username="clinician", password="correct-horse-battery"):
        user = db_session.query(User).filter_by(username=username).one()
        response = client.post(
            "/api/auth/login", json={"email": user.email, "password": password}
        )
        assert response.status_code == 200, response.text
        client.cookies.clear()
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login


@pytest.fixture
def token_for(db_session):
    """
    Mint a bearer header directly for a username.

    Lets one test hold headers for two accounts at once (e.g. a clinician and
    an administrator acting on the same request) without logging in twice.
    """
    from auth.security import create_access_token

    def _token(username):
        user = db_session.query(User).filter_by(username=username).one()
        return {
            "Authorization": f"Bearer {create_access_token(user.id, user.username, user.role)}"
        }

    return _token
