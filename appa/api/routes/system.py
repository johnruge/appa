"""System status and the kill switch, for the UI."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from appa import db
from appa.api.deps import SessionDep, UserDep

router = APIRouter(prefix="/system", tags=["system"])


@router.get("")
def status(s: SessionDep, _p: UserDep):
    """Return whether appa is paused, active runs, open questions, and spend against the daily cap."""
    from appa.llm import registry as models
    from appa.runtime.context import is_paused, spent_today

    active = s.scalar(select(func.count()).select_from(db.Run).where(db.Run.status.notin_(db.TERMINAL)))
    open_q = s.scalar(select(func.count()).select_from(db.Question).where(db.Question.status == "open"))
    cap = models.get_registry().limits.get("daily_usd_cap")
    return {
        "paused": is_paused(s),
        "active_runs": active,
        "open_questions": open_q,
        "spent_24h_usd": round(spent_today(s), 6),
        "daily_cap_usd": cap,
    }


@router.post("/stop-all")
def stop_all(s: SessionDep, _p: UserDep):
    """Kill switch: pause appa and cancel every active run."""
    from appa.runtime import runs

    return {"paused": True, "cancelled": runs.stop_all(s)}


@router.post("/resume")
def resume(s: SessionDep, _p: UserDep):
    """Unpause appa."""
    from appa.runtime.context import set_paused

    set_paused(s, False)
    return {"paused": False}
