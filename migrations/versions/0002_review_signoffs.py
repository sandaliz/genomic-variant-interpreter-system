"""add review_signoffs

Moves human-review sign-offs out of the local data/review_signoffs.json file and
into PostgreSQL, so an attestation has an owner (reviewed_by), survives restarts
and can be reported on.

The pre-existing JSON store, if present, is NOT imported automatically -- run
    python scripts/import_review_signoffs.py
to backfill it after upgrading.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('review_signoffs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('variant_id', sa.String(length=128), nullable=False),
    sa.Column('reviewer_name', sa.String(length=120), nullable=False),
    sa.Column('review_date', sa.Date(), nullable=False),
    sa.Column('signature', sa.String(length=120), nullable=True),
    sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reviewed_by', sa.Integer(), nullable=True),
    # SET NULL, not CASCADE: deleting an account must not erase a sign-off.
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('variant_id')
    )
    op.create_index(op.f('ix_review_signoffs_variant_id'), 'review_signoffs', ['variant_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema.

    Dropping this table loses every recorded sign-off, so export first if the
    data still matters.
    """
    op.drop_index(op.f('ix_review_signoffs_variant_id'), table_name='review_signoffs')
    op.drop_table('review_signoffs')