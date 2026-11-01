"""feeds: the listings inbox and discovered company boards

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30 23:30:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'listings',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('key', sa.String(length=500), nullable=False),
        sa.Column('company', sa.String(length=300), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('location', sa.String(length=500), nullable=False),
        sa.Column('url', sa.String(length=2000), nullable=False),
        sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('source_ref', sa.String(length=300), nullable=True),
        sa.Column('ats', sa.String(length=32), nullable=False),
        sa.Column('ats_job_id', sa.String(length=100), nullable=True),
        sa.Column('posted_at', sa.Date(), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('hints', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('application_id', sa.Integer(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('screened_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], name='fk_listings_application', ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key', name='uq_listings_key'),
    )
    op.create_index('ix_listings_status', 'listings', ['status'], unique=False)
    op.create_index('ix_listings_status_posted', 'listings', ['status', 'posted_at'], unique=False)
    op.create_table(
        'boards',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ats', sa.String(length=32), nullable=False),
        sa.Column('slug', sa.String(length=200), nullable=False),
        sa.Column('company', sa.String(length=300), nullable=False),
        sa.Column('found_via', sa.String(length=64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_count', sa.Integer(), nullable=True),
        sa.Column('kept_total', sa.Integer(), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ats', 'slug', name='uq_boards_ats_slug'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('boards')
    op.drop_index('ix_listings_status_posted', table_name='listings')
    op.drop_index('ix_listings_status', table_name='listings')
    op.drop_table('listings')
