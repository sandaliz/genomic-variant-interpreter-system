"""add query_history trace_id

Lets the /api/trail and /api/pipeline endpoints return only the caller's own
run when two accounts query the same variant_id. The MessageLog is keyed by
variant_id only, so without a per-run owner the variant-level check in
_history_for_user returned every run of an overlapping variant to each caller.

Nullable with no backfill: rows written before this migration simply cannot
scope a trail and read as 404 there, which is fail-closed. New rows carry the
trace_id written by record_history() at classify time.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('query_history', sa.Column('trace_id', sa.String(length=12), nullable=True))
    op.create_index('ix_query_history_user_variant_trace', 'query_history',
                    ['user_id', 'variant_id', 'trace_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_query_history_user_variant_trace', table_name='query_history')
    op.drop_column('query_history', 'trace_id')
