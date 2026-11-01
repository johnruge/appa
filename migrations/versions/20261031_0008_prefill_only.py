"""prefill only: appa never submits; old submit states move to the new lifecycle

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31 21:30:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: str | Sequence[str] | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade data (no schema change)."""
    op.execute("UPDATE apply_attempts SET status = 'ready' WHERE status = 'awaiting_approval'")
    op.execute(
        "UPDATE apply_attempts SET status = 'cancelled', error = 'appa no longer submits; prefill it again' "
        "WHERE status = 'submitting'"
    )
    op.execute("UPDATE queue_jobs SET status = 'done' WHERE kind = 'apply' AND status = 'queued' "
               "AND payload->>'phase' = 'submit'")
    op.execute("UPDATE questions SET kind = 'apply_ready' WHERE kind = 'apply_approval'")


def downgrade() -> None:
    """Downgrade data."""
    op.execute("UPDATE questions SET kind = 'apply_approval' WHERE kind = 'apply_ready'")
    op.execute("UPDATE apply_attempts SET status = 'awaiting_approval' WHERE status = 'ready'")
