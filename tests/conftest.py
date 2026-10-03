"""
Pytest fixtures shared by every test module.

The account/entitlement layer is tested against an in-memory SQLite database so
the suite stays fast and needs no running PostgreSQL. The model definitions are
deliberately portable (native_enum=False, generic column types) precisely so
this is possible; the production path is PostgreSQL via DATABASE_URL.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
              password="correct-horse-battery", is_active=True):
    """Insert a user row for tests, defaulting to a fresh Free plan."""
    from auth.security import hash_password

    if plan is None:
        plan = make_plan(session)
    user = User(
        username=username,
        email=f"{username}@example.com",
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
def login(client):
    """Log in through the real endpoint and return the bearer header."""

    def _login(username="clinician", password="correct-horse-battery"):
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login
