"""Recurring runs (cron, UTC)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from appa import db, llm
from appa.api.deps import SessionDep, UserDep
from appa.api.routes.runs import SERVER_PATH_FIELDS, Budget

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=32)
    cron: str = Field(min_length=9, max_length=100)
    model: str | None = Field(None, max_length=100)
    input: dict = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def sched_dict(x: db.Schedule) -> dict:
    """A schedule as a JSON-ready dict."""
    return {
        "id": x.id,
        "role": x.role,
        "cron": x.cron,
        "model": x.model,
        "input": x.input,
        "budget": x.budget,
        "enabled": x.enabled,
        "next_run_at": x.next_run_at.isoformat(),
        "last_run_at": x.last_run_at.isoformat() if x.last_run_at else None,
    }


@router.get("")
def list_(s: SessionDep, _p: UserDep):
    """List all schedules."""
    return {"schedules": [sched_dict(x) for x in s.scalars(select(db.Schedule).order_by(db.Schedule.id))]}


@router.post("", status_code=201)
def create(body: ScheduleIn, s: SessionDep, _p: UserDep):
    """Create a cron schedule for a role after checking its model and input."""
    from appa.agents import get_role
    from appa.llm import registry as models
    from appa.runtime import scheduler

    if SERVER_PATH_FIELDS & set(body.input):
        raise HTTPException(422, "scheduled runs can't take server file paths")
    try:
        if body.model:
            models.get_registry().resolve(get_role(body.role).model_role, body.model)
        x = scheduler.add(
            s,
            body.role,
            body.cron,
            model=body.model,
            input=body.input,
            budget=body.budget.model_dump(exclude_none=True),
        )
    except ValidationError as e:
        raise HTTPException(422, f"invalid input for {body.role}: {e.errors()[0]['msg']}") from e
    except (ValueError, llm.ModelNotAllowed) as e:
        raise HTTPException(422, str(e)) from e
    return sched_dict(x)


@router.patch("/{sid}")
def update(sid: int, body: SchedulePatch, s: SessionDep, _p: UserDep):
    """Enable or disable a schedule."""
    x = s.get(db.Schedule, sid)
    if not x:
        raise HTTPException(404, "not found")
    x.enabled = body.enabled
    s.commit()
    return sched_dict(x)


@router.delete("/{sid}")
def delete(sid: int, s: SessionDep, _p: UserDep):
    """Delete a schedule."""
    x = s.get(db.Schedule, sid)
    if not x:
        raise HTTPException(404, "not found")
    s.delete(x)
    s.commit()
    return {"ok": True}
