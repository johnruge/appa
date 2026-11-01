"""apply

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 23:28:27.705305

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'answers',
        sa.Column('key', sa.String(length=300), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table(
        'apply_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('ats', sa.String(length=32), nullable=True),
        sa.Column('apply_url', sa.String(length=2000), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('form', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('fill_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('preview_upload_id', sa.Integer(), nullable=True),
        sa.Column('confirmation_upload_id', sa.Integer(), nullable=True),
        sa.Column('final_url', sa.String(length=2000), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('approved_by', sa.String(length=64), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['confirmation_upload_id'], ['uploads.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['preview_upload_id'], ['uploads.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_apply_attempts_application_id'), 'apply_attempts', ['application_id'], unique=False)
    op.create_index(op.f('ix_apply_attempts_status'), 'apply_attempts', ['status'], unique=False)
    op.add_column(
        'queue_jobs',
        sa.Column(
            'payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )  # existing rows


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('queue_jobs', 'payload')
    op.drop_index(op.f('ix_apply_attempts_status'), table_name='apply_attempts')
    op.drop_index(op.f('ix_apply_attempts_application_id'), table_name='apply_attempts')
    op.drop_table('apply_attempts')
    op.drop_table('answers')
