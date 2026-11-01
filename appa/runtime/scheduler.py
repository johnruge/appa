"""Scheduler: turns due schedules into queued runs. One scheduler process is enough;
if two run, row locks keep a schedule from firing twice.

    appa scheduler
"""

from __future__ import annotations

import signal
import threading
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from appa import db
from appa.runtime import runs

TICK_S = 30.0


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


def next_time(cron: str, after: datetime) -> datetime:
    """The next time the cron expression fires after `after`; ValueError if the cron is invalid."""
    if not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression {cron!r} (5 fields, UTC, e.g. '0 */4 * * *')")
    return croniter(cron, after).get_next(datetime)


def add(
    s: Session, role: str, cron: str, *, model: str | None = None, input: dict | None = None, budget: dict | None = None
) -> db.Schedule:
    """Validate and store a schedule for a startable role, with its first run time; commits."""
    from appa.agents import get_role

    spec = get_role(role)
    if not spec.startable:
        raise ValueError(f"{role} can't be scheduled")
    params = spec.Input.model_validate(input or {})
    sched = db.Schedule(
        role=role,
        cron=cron,
        model=model,
        input=params.model_dump(mode="json"),
        budget=budget or {},
        next_run_at=next_time(cron, _now()),
    )
    s.add(sched)
    s.commit()
    return sched


def tick(s: Session, now: datetime | None = None) -> list[int]:
    """Fire every due schedule once. Returns the created run ids."""
    now = now or _now()
    created = []
    due = s.scalars(
        select(db.Schedule)
        .where(db.Schedule.enabled.is_(True), db.Schedule.next_run_at <= now)
        .with_for_update(skip_locked=True)
    ).all()
    for sched in due:
        run = runs.create(
            s,
            sched.role,
            model=sched.model,
            input=sched.input,
            budget=sched.budget,
            created_by=f"schedule:{sched.id}",
            schedule_id=sched.id,
        )
        sched.last_run_at = now
        sched.next_run_at = next_time(sched.cron, now)  # skip missed slots instead of piling up
        created.append(run.id)
    s.commit()
    return created


def loop(sessions: sessionmaker[Session] | None = None, log=None) -> None:
    """Queue due schedules every tick until SIGTERM or SIGINT."""
    log = log or (lambda msg: print(msg, flush=True))
    sessions = sessions or db.sessions()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log("[scheduler] started")
    while not stopping.is_set():
        with sessions() as s:
            for rid in tick(s):
                log(f"[scheduler] queued run #{rid}")
        stopping.wait(TICK_S)
    log("[scheduler] stopped")
