"""add password_resets + users.full_name

Supports the forgot-password flow (email -> 6-digit code -> new password) and the
profile page.

users.full_name is nullable, so existing accounts keep working unchanged and the
UI falls back to the username.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('full_name', sa.String(length=120), nullable=True))
    op.create_table('password_resets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('salt', sa.String(length=32), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_password_resets_user_created', 'password_resets',
                    ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the issued reset codes along with the table; that history is gone
    afterwards.
    """
    op.drop_index('ix_password_resets_user_created', table_name='password_resets')
    op.drop_table('password_resets')
    op.drop_column('users', 'full_name')