"""job ids from any source

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 14:54:17.565025

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('applications', sa.Column('job_id', sa.String(length=100), nullable=True))
    op.add_column('applications', sa.Column('job_id_source', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('applications', 'job_id_source')
    op.drop_column('applications', 'job_id')
