"""Concierge: turns everything waiting on you into questions, and reports a digest.
No model calls; free to run as often as you like."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from appa import db
from appa.agents.base import RoleSpec
from appa.runtime.context import RunContext

WAITING = ("Needs Review", "Apply Myself")


class ConciergeInput(BaseModel):
    since_hours: int = Field(168, ge=1, le=24 * 90)


def run(ctx: RunContext, params: ConciergeInput) -> dict:
    """Open a review question for each recent application waiting on you that has none; returns counts by status."""
    since = datetime.now(UTC) - timedelta(hours=params.since_hours)
    with ctx.sessions() as s:
        apps = list(
            s.scalars(
                select(db.Application)
                .where(db.Application.status.in_(WAITING), db.Application.created_at >= since)
                .order_by(db.Application.id)
            )
        )
        asked = set(
            s.scalars(
                select(db.Question.application_id).where(
                    db.Question.application_id.is_not(None), db.Question.status == "open"
                )
            )
        )
        open_questions = len(list(s.scalars(select(db.Question.id).where(db.Question.status == "open"))))
    new = 0
    for a in apps:
        if a.id in asked:
            continue
        ctx.check()
        ctx.steps += 1
        verb = "Apply to" if a.status == "Apply Myself" else "Review"
        ctx.ask(
            f"{verb} {a.company} / {a.role}"
            + (f" ({a.location})" if a.location else "")
            + (f": {a.notes[:300]}" if a.notes else ""),
            kind="review",
            application_id=a.id,
            context={"status": a.status, "link": a.link},
        )
        new += 1
    by_status: dict[str, int] = {}
    for a in apps:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    return {"waiting_on_you": by_status, "new_questions": new, "open_questions": open_questions + new}


ROLE = RoleSpec(
    name="concierge",
    description="Collect review items and apply-myself jobs into your question queue.",
    Input=ConciergeInput,
    run=run,
    uses_models=False,
    default_budget={"max_usd": 0.01, "max_steps": 2000, "max_model_calls": 1},
)
