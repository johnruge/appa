"""Create, execute, cancel and follow runs. Used by the CLI, the worker, the scheduler,
and (M4) the API, so a run behaves the same however it was started."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from appa import db
from appa.runtime import queue
from appa.runtime.context import RunContext, StopRun, set_paused


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


def create(
    s: Session,
    role: str,
    *,
    model: str | None = None,
    input: dict | None = None,
    budget: dict | None = None,
    created_by: str = "cli",
    schedule_id: int | None = None,
    enqueue: bool = True,
) -> db.Run:
    """Validate the input (and model) for a role, create the run, and queue it unless enqueue=False; commits."""
    from appa.agents import get_role

    spec = get_role(role)
    if not spec.startable and enqueue:
        raise ValueError(f"{role} runs are started by appa itself (e.g. `apply`), not directly")
    params = spec.Input.model_validate(input or {})  # reject bad input before anything runs
    if model is not None:
        from appa.llm import registry as models

        models.get_registry().resolve(spec.model_role, model, vision=False)  # must be allowlisted
    run = db.Run(
        role=role,
        model=model,
        input=params.model_dump(mode="json"),
        budget={**spec.default_budget, **(budget or {})},
        created_by=created_by,
        schedule_id=schedule_id,
    )
    s.add(run)
    s.flush()
    s.add(db.RunEvent(run_id=run.id, kind="status", payload={"status": "queued", "by": created_by}))
    if enqueue:
        queue.enqueue(s, "run", run.id)
    s.commit()
    return run


def execute(sessions: sessionmaker[Session], run_id: int, *, daily_cap_usd: float | None = None) -> db.Run:
    """Run a role to completion in this process. The worker calls this; so does --inline."""
    from appa.agents import get_role

    with sessions() as s:
        run = s.get(db.Run, run_id)
        if run.status in db.TERMINAL:
            return run
        if run.cancel_requested:
            return _finish(sessions, run_id, "cancelled", error="cancelled before start")
        run.status, run.started_at = "running", _now()
        s.commit()
        spec = get_role(run.role)
        ctx = RunContext(sessions, run, daily_cap_usd=daily_cap_usd)
        params = spec.Input.model_validate(run.input)
    ctx.event(
        "status", status="running", model=(ctx.model or f"default ({spec.model_role})") if spec.uses_models else "none"
    )
    try:
        ctx.check()
        result = spec.run(ctx, params)
    except StopRun as e:
        status = "cancelled"
        return _finish(sessions, run_id, status, error=str(e), ctx=ctx)
    except Exception as e:  # a role crashed: record it, never take the worker down
        return _finish(sessions, run_id, "failed", error=f"{e.__class__.__name__}: {str(e)[:1000]}", ctx=ctx)
    return _finish(sessions, run_id, "succeeded", result=result, ctx=ctx)


def _finish(
    sessions,
    run_id: int,
    status: str,
    *,
    error: str | None = None,
    result: dict | None = None,
    ctx: RunContext | None = None,
) -> db.Run:
    """Mark a run finished with its status, error, result and final counters, and log it; commits."""
    if ctx and result is not None:
        ctx.event("result", **result)
    with sessions() as s:
        run = s.get(db.Run, run_id)
        run.status, run.error, run.finished_at = status, error, _now()
        if result is not None:
            run.result = result
        if ctx:
            run.cost_usd, run.steps, run.model_calls = ctx.cost_usd, ctx.steps, ctx.model_calls
        s.add(
            db.RunEvent(
                run_id=run_id,
                kind="status",
                payload={
                    "status": status,
                    "error": error,
                    "cost_usd": round(run.cost_usd, 6),
                    "steps": run.steps,
                    "model_calls": run.model_calls,
                },
            )
        )
        s.commit()
        return run


def cancel(s: Session, run_id: int) -> db.Run | None:
    """Request cancellation; a run that never started is cancelled at once with its queue jobs. Commits."""
    run = s.get(db.Run, run_id)
    if not run or run.status in db.TERMINAL:
        return run
    run.cancel_requested = True
    if run.status == "queued":  # never started: finish it now and retire its queue entry
        run.status, run.finished_at, run.error = "cancelled", _now(), "cancelled before start"
        s.add(db.RunEvent(run_id=run_id, kind="status", payload={"status": "cancelled"}))
        for job in s.scalars(select(db.QueueJob).where(db.QueueJob.run_id == run_id, db.QueueJob.status == "queued")):
            job.status = "done"
    s.commit()
    return run


def stop_all(s: Session) -> int:
    """Kill switch: pause the system and cancel every active run."""
    set_paused(s, True)
    active = list(s.scalars(select(db.Run.id).where(db.Run.status.notin_(db.TERMINAL))))
    for rid in active:
        cancel(s, rid)
    return len(active)


def follow(
    sessions: sessionmaker[Session],
    run_id: int,
    *,
    poll_s: float = 0.4,
    timeout_s: float | None = None,
    give_up_if_queued_s: float | None = None,
) -> Iterator[db.RunEvent]:
    """Yield a run's events as they're written, until the run finishes (or nobody starts it)."""
    last_id, started = 0, time.monotonic()
    while True:
        with sessions() as s:
            events = list(
                s.scalars(
                    select(db.RunEvent)
                    .where(db.RunEvent.run_id == run_id, db.RunEvent.id > last_id)
                    .order_by(db.RunEvent.id)
                )
            )
            status = s.scalar(select(db.Run.status).where(db.Run.id == run_id))
        for ev in events:
            last_id = ev.id
            yield ev
        if status in db.TERMINAL and not events:
            return
        waited = time.monotonic() - started
        if timeout_s is not None and waited > timeout_s:
            return
        if give_up_if_queued_s is not None and status == "queued" and waited > give_up_if_queued_s:
            return
        time.sleep(poll_s)
