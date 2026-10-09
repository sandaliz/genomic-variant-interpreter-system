"""add upgrade_requests

Models the upgrade hand-off: a user asks for a paid plan, an administrator
reviews and decides, and the decision is recorded. Approving a request applies
the plan change.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('upgrade_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('target_plan_id', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    # native_enum=False keeps this a VARCHAR + CHECK so the same definition
    # works on PostgreSQL and on the SQLite the unit tests run against.
    sa.Column('status', sa.Enum('pending', 'contacted', 'approved', 'declined',
                                name='upgrade_status', native_enum=False), nullable=False),
    sa.Column('admin_note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['target_plan_id'], ['plans.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_upgrade_user_created', 'upgrade_requests',
                    ['user_id', 'created_at'], unique=False)
    op.create_index('ix_upgrade_status_created', 'upgrade_requests',
                    ['status', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_upgrade_status_created', table_name='upgrade_requests')
    op.drop_index('ix_upgrade_user_created', table_name='upgrade_requests')
    op.drop_table('upgrade_requests')