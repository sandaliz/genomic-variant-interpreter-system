"""
db/database.py

SQLAlchemy engine and session factory for the account/entitlement layer.

DATABASE_URL comes from the environment (see .env / .env.example). This module
is the only place that knows how to reach the database -- the four agents never
import it, so the agentic core stays independent of persistence.

Environment:
    DATABASE_URL   postgresql+psycopg://user:pass@localhost:5433/variantlens
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Local development default -- matches docker-compose.yml. The host port is 5433
# because 5432 is commonly taken by a locally installed PostgreSQL server.
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://variantlens:development_password@localhost:5433/variantlens"
)

DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

# pool_pre_ping avoids handing out connections that PostgreSQL has already
# closed (e.g. after a container restart during development).
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in db/models.py."""


def get_db():
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
