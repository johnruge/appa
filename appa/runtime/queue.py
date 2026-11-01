"""Job queue in Postgres. No Redis.

claim() takes the oldest ready job with SELECT ... FOR UPDATE SKIP LOCKED, so any number
of workers can poll safely: each job goes to exactly one of them. A claimed job holds a
lease; if its worker dies, the lease expires and another worker picks the job up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from appa.db import QueueJob

CHANNEL = "appa_jobs"  # NOTIFY channel: wakes idle workers immediately
LEASE = timedelta(minutes=10)


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


def enqueue(s: Session, kind: str, run_id: int | None = None, *, delay_s: float = 0, max_attempts: int = 3) -> QueueJob:
    """Add a queue job and notify waiting workers (the caller commits)."""
    job = QueueJob(
        kind=kind, run_id=run_id, max_attempts=max_attempts, available_at=_now() + timedelta(seconds=delay_s)
    )
    s.add(job)
    s.flush()
    s.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": str(job.id)})
    return job


def claim(s: Session, worker: str, kinds: tuple[str, ...] = ("run",)) -> QueueJob | None:
    """Lease the oldest available job of these kinds (or one whose lease expired), skipping locked rows; commits."""
    now = _now()
    job = s.scalars(
        select(QueueJob)
        .where(QueueJob.kind.in_(kinds))
        .where(
            or_(
                (QueueJob.status == "queued") & (QueueJob.available_at <= now),
                (QueueJob.status == "leased") & (QueueJob.lease_until < now),  # abandoned by a dead worker
            )
        )
        .order_by(QueueJob.available_at, QueueJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if not job:
        s.rollback()
        return None
    job.status, job.leased_by, job.lease_until = "leased", worker, now + LEASE
    job.attempts += 1
    s.commit()
    return job


def extend(s: Session, job: QueueJob) -> None:
    """Renew the job's lease and commit."""
    job.lease_until = _now() + LEASE
    s.commit()


def done(s: Session, job: QueueJob) -> None:
    """Mark the job done and commit."""
    job.status, job.lease_until = "done", None
    s.commit()


def fail(s: Session, job: QueueJob, error: str, *, retry: bool) -> None:
    """Retry with backoff (30s, 2m, 8m, ...) until max_attempts, then give up."""
    job.last_error = error[:2000]
    if retry and job.attempts < job.max_attempts:
        job.status, job.lease_until = "queued", None
        job.available_at = _now() + timedelta(seconds=30 * 4 ** (job.attempts - 1))
    else:
        job.status, job.lease_until = "failed", None
    s.commit()
