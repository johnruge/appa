"""runtime: runs, events, queue, schedules, questions, settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 22:45:02.325684

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'schedules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('cron', sa.String(length=100), nullable=False),
        sa.Column('input', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('budget', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_schedules_next_run_at'), 'schedules', ['next_run_at'], unique=False)
    op.create_table(
        'settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table(
        'runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('input', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('budget', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('cost_usd', sa.Float(), nullable=False),
        sa.Column('steps', sa.Integer(), nullable=False),
        sa.Column('model_calls', sa.Integer(), nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('schedule_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_runs_role'), 'runs', ['role'], unique=False)
    op.create_index(op.f('ix_runs_status'), 'runs', ['status'], unique=False)
    op.create_table(
        'questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('application_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_questions_run_id'), 'questions', ['run_id'], unique=False)
    op.create_index(op.f('ix_questions_status'), 'questions', ['status'], unique=False)
    op.create_table(
        'queue_jobs',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('leased_by', sa.String(length=100), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_queue_jobs_ready', 'queue_jobs', ['status', 'available_at'], unique=False)
    op.create_table(
        'run_events',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_run_events_run_id_id', 'run_events', ['run_id', 'id'], unique=False)
    op.add_column('seen_jobs', sa.Column('run_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_seen_jobs_run_id'), 'seen_jobs', ['run_id'], unique=False)
    op.create_foreign_key('fk_seen_jobs_run_id', 'seen_jobs', 'runs', ['run_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    # WARNING: constraint name is None; this directive will fail as
    # rendered.  Add a name, or use a naming convention; see
    # https://alembic.sqlalchemy.org/en/latest/naming.html
    op.drop_constraint('fk_seen_jobs_run_id', 'seen_jobs', type_='foreignkey')
    op.drop_index(op.f('ix_seen_jobs_run_id'), table_name='seen_jobs')
    op.drop_column('seen_jobs', 'run_id')
    op.drop_index('ix_run_events_run_id_id', table_name='run_events')
    op.drop_table('run_events')
    op.drop_index('ix_queue_jobs_ready', table_name='queue_jobs')
    op.drop_table('queue_jobs')
    op.drop_index(op.f('ix_questions_status'), table_name='questions')
    op.drop_index(op.f('ix_questions_run_id'), table_name='questions')
    op.drop_table('questions')
    op.drop_index(op.f('ix_runs_status'), table_name='runs')
    op.drop_index(op.f('ix_runs_role'), table_name='runs')
    op.drop_table('runs')
    op.drop_table('settings')
    op.drop_index(op.f('ix_schedules_next_run_at'), table_name='schedules')
    op.drop_table('schedules')
