"""System status and the kill switch, for the UI."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from appa import db
from appa.api.deps import SessionDep, UserDep

router = APIRouter(prefix="/system", tags=["system"])
MATCH_STATUSES = ("To Apply", "Needs Review", "Apply Myself")


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


@router.get("/overview")
def overview(s: SessionDep, _p: UserDep):
    """Everything the home screen shows in one call: matches waiting, what you applied to, the inbox, the last
    and next sweep, and spend."""
    from datetime import UTC, datetime, timedelta

    from appa.runtime.context import spent_today

    now = datetime.now(UTC)
    by_status = dict(s.execute(select(db.Application.status, func.count()).group_by(db.Application.status)).all())
    new_24h = s.scalar(
        select(func.count())
        .select_from(db.Application)
        .where(db.Application.created_at >= now - timedelta(hours=24), db.Application.status.in_(MATCH_STATUSES))
    )
    applied_7d = s.scalar(
        select(func.count())
        .select_from(db.Application)
        .where(db.Application.status == "Applied", db.Application.date_applied >= (now - timedelta(days=7)).date())
    )
    inbox = dict(s.execute(select(db.FeedListing.status, func.count()).group_by(db.FeedListing.status)).all())
    boards = s.scalar(select(func.count()).select_from(db.Board).where(db.Board.enabled.is_(True)))
    last = s.scalars(select(db.Run).where(db.Run.role == "scout").order_by(db.Run.id.desc()).limit(1)).first()
    nxt = s.scalars(
        select(db.Schedule).where(db.Schedule.enabled.is_(True)).order_by(db.Schedule.next_run_at).limit(1)
    ).first()
    questions = dict(
        s.execute(
            select(db.Question.kind, func.count()).where(db.Question.status == "open").group_by(db.Question.kind)
        ).all()
    )
    return {
        "matches": {status: by_status.get(status, 0) for status in MATCH_STATUSES},
        "new_matches_24h": new_24h,
        "applied_total": sum(by_status.get(x, 0) for x in db.APPLIED_STATUSES),
        "applied_7d": applied_7d,
        "by_status": by_status,
        "inbox": inbox,
        "boards": boards,
        "last_sweep": (
            {
                "id": last.id,
                "status": last.status,
                "started_at": last.created_at.isoformat(),
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                "cost_usd": round(last.cost_usd or 0, 4),
                "result": last.result,
            }
            if last
            else None
        ),
        "next_sweep_at": nxt.next_run_at.isoformat() if nxt else None,
        "questions": questions,
        "spent_24h_usd": round(spent_today(s), 4),
    }
