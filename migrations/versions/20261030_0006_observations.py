"""observations: misses and failures, for improving appa

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 22:10:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'observations',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('site', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=True),
        sa.Column('attempt_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], name='fk_observations_application', ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['attempt_id'], ['apply_attempts.id'], name='fk_observations_attempt', ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_observations_kind_created', 'observations', ['kind', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_observations_kind_created', table_name='observations')
    op.drop_table('observations')
