"""add query_history

The per-account query log: one row per accepted /api/classify call, holding the
question and the summary of the answer. UsageRecord keeps the quota arithmetic
and stores no content; this table is what lets a clinician reopen a variant they
ran last week, and what gives an administrator a real query log per account
instead of only a count.

Also extends the auth_events enum with `admin_read_history`, so reading another
account's queries is recorded like any other privileged action. On PostgreSQL
that enum is a VARCHAR plus a CHECK constraint, so the constraint is replaced
rather than merely rewritten in Python.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The full allowlist, matching db.models.AUTH_EVENT_TYPES as of this revision.
AUTH_EVENT_TYPES = (
    "login_success",
    "login_failed",
    "login_rate_limited",
    "logout",
    "register",
    "password_changed",
    "password_reset_requested",
    "password_reset_completed",
    "role_changed",
    "plan_changed",
    "account_activated",
    "account_deactivated",
    "admin_password_reset",
    "admin_read_history",
)

# SQLAlchemy renders a non-native Enum as VARCHAR + a CHECK constraint. The
# constraint's name is not guaranteed across versions, so it is located through
# the catalogue instead of assumed: dropping the wrong name would either fail or
# silently leave the old list in place, which is worse.
_FIND_EVENT_TYPE_CHECKS = """
SELECT conname
FROM pg_constraint
WHERE conrelid = 'auth_events'::regclass
  AND contype = 'c'
  AND pg_get_constraintdef(oid) LIKE '%event_type%'
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'query_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('query_text', sa.String(length=1000), nullable=False),
        sa.Column('variant_id', sa.String(length=128), nullable=True),
        sa.Column('gene', sa.String(length=32), nullable=True),
        sa.Column('classification', sa.String(length=64), nullable=True),
        sa.Column('confidence', sa.String(length=32), nullable=True),
        sa.Column('confidence_numeric', sa.Float(), nullable=True),
        sa.Column('reasoning_mode', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_query_history_user_created', 'query_history',
                    ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_query_history_request_id'), 'query_history',
                    ['request_id'], unique=True)

    # The new event type has to be inside the CHECK, or the audit insert for an
    # admin reading a query log is rejected. Skipped on SQLite, which has no
    # enum constraint to widen (and is only ever a test database).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in [row[0] for row in bind.execute(sa.text(_FIND_EVENT_TYPE_CHECKS))]:
            op.drop_constraint(name, 'auth_events', type_='check')
        allowed = ", ".join(f"'{value}'" for value in AUTH_EVENT_TYPES)
        op.create_check_constraint(
            'auth_event_type', 'auth_events', f"event_type IN ({allowed})"
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in [row[0] for row in bind.execute(sa.text(_FIND_EVENT_TYPE_CHECKS))]:
            op.drop_constraint(name, 'auth_events', type_='check')
        allowed = ", ".join(
            f"'{value}'" for value in AUTH_EVENT_TYPES if value != "admin_read_history"
        )
        op.create_check_constraint(
            'auth_event_type', 'auth_events', f"event_type IN ({allowed})"
        )

    op.drop_index(op.f('ix_query_history_request_id'), table_name='query_history')
    op.drop_index('ix_query_history_user_created', table_name='query_history')
    op.drop_table('query_history')
