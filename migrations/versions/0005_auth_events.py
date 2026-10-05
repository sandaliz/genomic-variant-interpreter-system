"""add auth_events

An append-only record of security-relevant actions: sign-ins, failures,
throttling, and administrator changes. Lets "who signed in, from where, and what
did an admin change?" be answered after the fact.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('auth_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.Enum(*AUTH_EVENT_TYPES, name='auth_event_type',
               native_enum=False, validate_strings=True), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('succeeded', sa.Boolean(), nullable=False),
    sa.Column('identifier_hash', sa.String(length=64), nullable=True),
    sa.Column('client_ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=256), nullable=True),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
              nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_auth_events_type_created', 'auth_events',
                    ['event_type', 'created_at'], unique=False)
    op.create_index('ix_auth_events_user_created', 'auth_events',
                    ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_auth_events_user_created', table_name='auth_events')
    op.drop_index('ix_auth_events_type_created', table_name='auth_events')
    op.drop_table('auth_events')