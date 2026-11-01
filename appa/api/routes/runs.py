"""Start, watch and stop agent runs."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from appa import db, llm
from appa.api.deps import Principal, SessionDep, require

router = APIRouter(prefix="/runs", tags=["runs"])

# Inputs that name files on the server are CLI-only. Over HTTP, screenshots are upload ids.
SERVER_PATH_FIELDS = {"screenshots"}
STREAM_MAX_S = 3600
STREAM_POLL_S = 0.5
KEEPALIVE_S = 15


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_usd: float | None = Field(None, gt=0, le=20)
    max_steps: int | None = Field(None, ge=1, le=10_000)
    max_model_calls: int | None = Field(None, ge=0, le=5_000)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=32)
    model: str | None = Field(None, max_length=100)
    input: dict = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)


def run_dict(r: db.Run) -> dict:
    """A run as a JSON-ready dict."""
    return {
        "id": r.id,
        "role": r.role,
        "model": r.model,
        "status": r.status,
        "input": r.input,
        "budget": r.budget,
        "cost_usd": round(r.cost_usd or 0, 6),
        "steps": r.steps,
        "model_calls": r.model_calls,
        "result": r.result,
        "error": r.error,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat(),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def event_dict(e: db.RunEvent) -> dict:
    """A run event as a JSON-ready dict."""
    return {"id": e.id, "ts": e.ts.isoformat(), "kind": e.kind, "payload": e.payload}


@router.post("", status_code=201)
def create(body: RunIn, s: SessionDep, p: Annotated[Principal, Depends(require("runs:write"))]):
    """Start a run of an agent role; screenshots must come as upload ids, not server paths."""
    from appa.runtime import runs

    if SERVER_PATH_FIELDS & set(body.input):
        raise HTTPException(422, "screenshots must be sent as upload_ids (upload them first)")
    for uid in body.input.get("upload_ids", []) or []:
        if not isinstance(uid, int) or not s.get(db.Upload, uid):
            raise HTTPException(422, f"unknown upload id {uid!r}")
    try:
        run = runs.create(
            s,
            body.role,
            model=body.model,
            input=body.input,
            budget=body.budget.model_dump(exclude_none=True),
            created_by=p.label,
        )
    except ValidationError as e:
        raise HTTPException(422, f"invalid input for {body.role}: {e.errors()[0]['msg']}") from e
    except (ValueError, llm.ModelNotAllowed) as e:
        raise HTTPException(422, str(e)) from e
    return run_dict(run)


@router.get("")
def list_(
    s: SessionDep,
    _p: Annotated[Principal, Depends(require("runs:read"))],
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=200),
):
    """List runs, newest first, optionally by status."""
    stmt = select(db.Run).order_by(db.Run.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(db.Run.status == status)
    return {"runs": [run_dict(r) for r in s.scalars(stmt)]}


@router.get("/{run_id}")
def get_one(run_id: int, s: SessionDep, _p: Annotated[Principal, Depends(require("runs:read"))]):
    """Return one run (404 if missing)."""
    r = s.get(db.Run, run_id)
    if not r:
        raise HTTPException(404, "not found")
    return run_dict(r)


@router.get("/{run_id}/events")
def events(
    run_id: int,
    s: SessionDep,
    _p: Annotated[Principal, Depends(require("runs:read"))],
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
):
    """Return a run's events after a given event id, oldest first."""
    if not s.get(db.Run, run_id):
        raise HTTPException(404, "not found")
    rows = s.scalars(
        select(db.RunEvent)
        .where(db.RunEvent.run_id == run_id, db.RunEvent.id > after)
        .order_by(db.RunEvent.id)
        .limit(limit)
    )
    return {"events": [event_dict(e) for e in rows]}


@router.post("/{run_id}/cancel")
def cancel(run_id: int, s: SessionDep, _p: Annotated[Principal, Depends(require("runs:write"))]):
    """Cancel a run (404 if missing)."""
    from appa.runtime import runs

    r = runs.cancel(s, run_id)
    if not r:
        raise HTTPException(404, "not found")
    return run_dict(r)


@router.get("/{run_id}/stream")
def stream(run_id: int, request: Request, s: SessionDep, _p: Annotated[Principal, Depends(require("runs:read"))]):
    """Server-Sent Events: every event as it's written, then `end` when the run finishes.
    Reconnecting clients send Last-Event-ID and resume where they left off."""
    if not s.get(db.Run, run_id):
        raise HTTPException(404, "not found")
    try:
        after = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        after = 0
    return StreamingResponse(
        _sse(run_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(run_id: int, after: int) -> Iterator[str]:
    """Poll a run's new events and yield them as server-sent events, with keepalives, until it ends or times out."""
    sessions = db.sessions()
    started = last_send = time.monotonic()
    while time.monotonic() - started < STREAM_MAX_S:
        with sessions() as s:
            rows = list(
                s.scalars(
                    select(db.RunEvent)
                    .where(db.RunEvent.run_id == run_id, db.RunEvent.id > after)
                    .order_by(db.RunEvent.id)
                    .limit(500)
                )
            )
            status = s.scalar(select(db.Run.status).where(db.Run.id == run_id))
        for e in rows:
            after = e.id
            yield f"id: {e.id}\nevent: {e.kind}\ndata: {json.dumps(event_dict(e))}\n\n"
            last_send = time.monotonic()
        if status in db.TERMINAL and not rows:
            yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
            return
        if time.monotonic() - last_send > KEEPALIVE_S:
            yield ": keepalive\n\n"
            last_send = time.monotonic()
        time.sleep(STREAM_POLL_S)
