"""add api_keys

Long-lived credentials for programmatic access, gated by the plan's api_access
entitlement. Only the SHA-256 digest of a key is stored; `prefix` keeps the first
characters visible so a user can tell keys apart.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_api_keys_user_created', 'api_keys',
                    ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys',
                    ['key_hash'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_index('ix_api_keys_user_created', table_name='api_keys')
    op.drop_table('api_keys')
