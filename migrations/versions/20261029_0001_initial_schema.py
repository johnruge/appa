"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-29 22:14:56.559505

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('scopes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_table(
        'applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=300), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('stage', sa.String(length=100), nullable=True),
        sa.Column('link', sa.String(length=2000), nullable=True),
        sa.Column('location', sa.String(length=300), nullable=True),
        sa.Column('date_applied', sa.Date(), nullable=True),
        sa.Column('applied_with_email', sa.String(length=254), nullable=True),
        sa.Column('referral', sa.Boolean(), nullable=True),
        sa.Column('people', sa.String(length=500), nullable=True),
        sa.Column('todo', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('job_key', sa.String(length=300), nullable=True),
        sa.Column('company_norm', sa.String(length=200), nullable=False),
        sa.Column('role_norm', sa.String(length=300), nullable=False),
        sa.Column('import_key', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('import_key'),
        sa.UniqueConstraint('job_key'),
    )
    op.create_index(op.f('ix_applications_company_norm'), 'applications', ['company_norm'], unique=False)
    op.create_index(op.f('ix_applications_status'), 'applications', ['status'], unique=False)
    op.create_table(
        'seen_jobs',
        sa.Column('key', sa.String(length=2000), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('url', sa.String(length=2000), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('verdict', sa.String(length=32), nullable=False),
        sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('extraction', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('application_id', sa.Integer(), nullable=True),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(op.f('ix_seen_jobs_verdict'), 'seen_jobs', ['verdict'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_seen_jobs_verdict'), table_name='seen_jobs')
    op.drop_table('seen_jobs')
    op.drop_index(op.f('ix_applications_status'), table_name='applications')
    op.drop_index(op.f('ix_applications_company_norm'), table_name='applications')
    op.drop_table('applications')
    op.drop_table('api_keys')
