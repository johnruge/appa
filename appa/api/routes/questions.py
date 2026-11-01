"""Your question queue: review items, apply-myself jobs, things a run couldn't decide."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from appa import db
from appa.api.deps import SessionDep, UserDep

router = APIRouter(prefix="/questions", tags=["questions"])

# What each answer does to the linked application.
ACTION_STATUS = {"approve": "To Apply", "apply_myself": "Apply Myself", "skip": "Skipped"}
UNCONFIRMED_STATUS = {"approve": "Applied", "skip": "To Apply"}  # "did it go through?" questions


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "apply_myself", "skip", "dismiss", "choose", "answer", "applied"]
    choice: str | None = Field(None, max_length=5000)  # "choose": an offered URL; "answer": your answer
    note: str | None = Field(None, max_length=2000)


def q_dict(q: db.Question) -> dict:
    """A question as a JSON-ready dict."""
    return {
        "id": q.id,
        "kind": q.kind,
        "prompt": q.prompt,
        "context": q.context,
        "status": q.status,
        "application_id": q.application_id,
        "run_id": q.run_id,
        "answer": q.answer,
        "created_at": q.created_at.isoformat(),
    }


@router.get("")
def list_(
    s: SessionDep,
    _p: UserDep,
    status: Literal["open", "answered", "dismissed"] = "open",
    limit: int = Query(100, ge=1, le=500),
):
    """List questions with the given status (open by default), oldest first."""
    rows = s.scalars(select(db.Question).where(db.Question.status == status).order_by(db.Question.id).limit(limit))
    return {"questions": [q_dict(q) for q in rows]}


@router.post("/{qid}/answer")
def answer(qid: int, body: AnswerIn, s: SessionDep, p: UserDep):
    """Answer an open question: set the application's status, pick a posting to screen, or hand off apply questions."""
    from appa.runtime import runs

    q = s.get(db.Question, qid)
    if not q:
        raise HTTPException(404, "not found")
    if q.status != "open":
        raise HTTPException(409, f"question already {q.status}")
    out: dict = {}
    if q.kind == "apply_field":
        return _answer_apply_field(s, q, body, p)
    if q.kind == "apply_ready":
        return _answer_apply_ready(s, q, body, p)
    if body.action == "applied":
        raise HTTPException(422, "applied is for prefilled applications")
    if body.action == "answer":
        raise HTTPException(422, "answer is for form questions")
    if body.action in ACTION_STATUS:
        if not q.application_id:
            raise HTTPException(422, f"{body.action} needs a question about an application")
        app = s.get(db.Application, q.application_id)
        if q.context.get("unconfirmed") and body.action in UNCONFIRMED_STATUS:
            app.status = UNCONFIRMED_STATUS[body.action]
            if body.action == "approve":
                from datetime import date

                app.date_applied = app.date_applied or date.today()
        else:
            app.status = ACTION_STATUS[body.action]
        if body.note:
            app.notes = f"{app.notes}; {body.note}" if app.notes else body.note
    elif body.action == "choose":
        offered = {o.get("url") for o in (q.context or {}).get("options", [])}
        if not body.choice or body.choice not in offered:
            raise HTTPException(422, "choice must be one of the offered postings")
        run = runs.create(s, "screener", input={"urls": [body.choice]}, created_by=p.label)
        out["run_id"] = run.id
    q.status = "dismissed" if body.action == "dismiss" else "answered"
    q.answer = body.choice or body.action
    q.answered_at = datetime.now(UTC)
    s.commit()
    return {**q_dict(q), **out}


def _answer_apply_field(s, q: db.Question, body: AnswerIn, p) -> dict:
    """Answer a form-field question and resume its attempts; dismissing it cancels the attempt."""
    from appa.apply import service

    if body.action == "dismiss":
        attempt_id = q.context.get("attempt_id")
        q.status, q.answered_at = "dismissed", datetime.now(UTC)
        s.commit()
        if attempt_id:
            service.cancel(s, attempt_id)  # can't fill it without this answer: you'll apply yourself
        return {**q_dict(q), "attempt_cancelled": attempt_id}
    if body.action != "answer" or not body.choice:
        raise HTTPException(422, "send action=answer with your answer in choice")
    try:
        resumed = service.answer_field(s, q, body.choice, p.label)
    except service.ApplyError as e:
        raise HTTPException(422, str(e)) from e
    return {**q_dict(q), "resumed_attempts": resumed}


def _answer_apply_ready(s, q: db.Question, body: AnswerIn, p) -> dict:
    """The form is filled in your browser: you submitted it ("applied"), or you're not going ahead ("skip")."""
    from appa.apply import service

    attempt_id = q.context.get("attempt_id")
    if body.action == "applied":
        try:
            service.mark_applied(s, q.application_id, p.label)
        except service.ApplyError as e:
            raise HTTPException(422, str(e)) from e
    elif body.action in ("skip", "dismiss"):
        service.cancel(s, attempt_id)
    else:
        raise HTTPException(422, "say whether you submitted it (applied) or skip it")
    s.refresh(q)
    return q_dict(q)
