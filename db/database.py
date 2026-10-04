"""
db/database.py

SQLAlchemy engine and session factory for the account/entitlement layer.

DATABASE_URL comes from the environment (see .env / .env.example). This module
is the only place that knows how to reach the database -- the four agents never
import it, so the agentic core stays independent of persistence.

There is deliberately NO fallback URL. An earlier version defaulted to a local
`localhost:5433` PostgreSQL, which was a trap: a teammate with no database running
got a connection refused error with nothing pointing at the real fix. The one
database setting is now required and named.

Environment:
    DATABASE_URL   a PostgreSQL connection string, e.g. from Neon:
                   postgresql://USER:PASSWORD@ep-xxx.region.aws.neon.tech/neondb?sslmode=require

                   Any PostgreSQL works -- Neon, a local install, an institutional
                   server. Nothing else about the app cares which.

Note: the test suite never touches this engine (tests/conftest.py overrides
get_db with in-memory SQLite and sets a placeholder URL before importing this
module), so `pytest` still runs with no database at all.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

_MISSING_URL_MESSAGE = (
    "DATABASE_URL is not set.\n\n"
    "VariantLens stores accounts, plans, usage and sign-offs in PostgreSQL.\n"
    "Create a free project at https://neon.com, copy its connection string, and\n"
    "add this line to your .env:\n\n"
    "    DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require\n\n"
    "Then initialise the schema:\n\n"
    "    alembic upgrade head\n"
    "    python scripts/seed_plans.py\n"
    "    python scripts/create_admin.py\n"
)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(_MISSING_URL_MESSAGE)

# pool_pre_ping avoids handing out a connection the server has already closed --
# worth keeping now that the database is hosted and may have scaled to zero.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in db/models.py."""


def _is_connection_dead(exc: Exception) -> bool:
    """
    True only for "the socket/connection went away", never for a query that
    actually ran and was rejected (constraint violation, bad syntax, deadlock).
    """
    # psycopg reports a severed connection as InterfaceError, which SQLAlchemy
    # re-raises as its own InterfaceError (a sibling of OperationalError, not a
    # subclass -- so both have to be named here).
    if exc.__class__.__name__ == "InterfaceError":
        return True
    original = getattr(exc, "orig", None)
    if original is None:
        return False
    if original.__class__.__name__ == "InterfaceError":
        return True
    text = str(original).lower()
    return any(marker in text for marker in (
        "server closed the connection unexpectedly",
        "connection is closed",
        "connection already closed",
        "terminating connection",
        "could not connect",
        "connection reset",
        "broken pipe",
    ))


def get_db():
    """
    FastAPI dependency that yields a session and always closes it.

    pool_pre_ping only guards the *checkout*: if the server drops the connection
    while a statement is already in flight, the caller still sees an
    OperationalError and the request 500s. Neon suspends an idle compute after a
    few minutes and occasionally severs a connection on wake, so that window is
    real rather than theoretical -- it showed up as a blank admin dashboard.

    One retry covers it. The retry is only safe for the failures it targets: a
    connection that died before doing any work has not changed any rows.
    """
    db = SessionLocal()
    try:
        yield db
    except (OperationalError, InterfaceError) as exc:
        if not _is_connection_dead(exc):
            raise
        db.rollback()
        db.close()
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    finally:
        db.close()
