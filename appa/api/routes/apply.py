"""Prefill applications in your browser, track attempts, mark what you applied to, manage your answers."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from appa import db, uploads
from appa.api.deps import SessionDep, UserDep
from appa.apply import service

router = APIRouter(tags=["apply"])

STANDARD_KEYS = (
    "first_name",
    "last_name",
    "preferred_name",
    "email",
    "phone",
    "location",
    "country",
    "linkedin_url",
    "github_url",
    "website_url",
    "current_company",
    "school",
    "degree",
    "graduation_date",
    "gpa",
    "work_authorized",
    "requires_sponsorship",
    "sponsorship_now",
    "previously_employed",
    "how_heard",
    "work_location",
    "start_date",
    "pronouns",
    "gender",
    "race",
    "hispanic_latino",
    "veteran_status",
    "disability_status",
    "sms_consent",
    "whatsapp_consent",
    "salary_expectations",
    "cover_letter_on_file",
)


def attempt_dict(a: db.ApplyAttempt) -> dict:
    """An apply attempt as a JSON-ready dict."""
    return {
        "id": a.id,
        "application_id": a.application_id,
        "run_id": a.run_id,
        "ats": a.ats,
        "apply_url": a.apply_url,
        "status": a.status,
        "plan": a.plan,
        "fill_report": a.fill_report,
        "preview_upload_id": a.preview_upload_id,
        "final_url": a.final_url,
        "error": a.error,
        "applied_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "created_at": a.created_at.isoformat(),
    }


@router.post("/applications/{app_id}/apply", status_code=201)
def start(app_id: int, s: SessionDep, p: UserDep):
    """Start an automated application for a tracked job (422 if it can't be started)."""
    try:
        return attempt_dict(service.start(s, app_id, p.label))
    except service.ApplyError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/applications/{app_id}/takeover")
def takeover(app_id: int, s: SessionDep, p: UserDep):
    """You apply to this one yourself from scratch: stops any prefill and marks it Apply Myself (humans only)."""
    try:
        app, link = service.takeover(s, app_id, p.label)
    except service.ApplyError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": app.id, "status": app.status, "link": link}


@router.get("/apply/attempts")
def list_(
    s: SessionDep, _p: UserDep, status: str | None = Query(None, max_length=20), limit: int = Query(50, ge=1, le=200)
):
    """List apply attempts, newest first, optionally by status."""
    stmt = select(db.ApplyAttempt).order_by(db.ApplyAttempt.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(db.ApplyAttempt.status == status)
    return {"attempts": [attempt_dict(a) for a in s.scalars(stmt)]}


@router.get("/apply/attempts/{attempt_id}")
def get_one(attempt_id: int, s: SessionDep, _p: UserDep):
    """Return one apply attempt (404 if missing)."""
    a = s.get(db.ApplyAttempt, attempt_id)
    if not a:
        raise HTTPException(404, "not found")
    return attempt_dict(a)


@router.post("/applications/{app_id}/applied")
def applied(app_id: int, s: SessionDep, p: UserDep):
    """You submitted it yourself: mark it Applied and close its prefill (humans only)."""
    try:
        app = service.mark_applied(s, app_id, p.label)
    except service.ApplyError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": app.id, "status": app.status, "date_applied": app.date_applied}


@router.post("/apply/attempts/{attempt_id}/redo")
def redo(attempt_id: int, s: SessionDep, p: UserDep):
    """Fill the form again from the current answer bank (never submits)."""
    try:
        return attempt_dict(service.redo(s, attempt_id, p.label))
    except service.ApplyError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/apply/attempts/{attempt_id}/cancel")
def cancel(attempt_id: int, s: SessionDep, _p: UserDep):
    """Cancel an apply attempt (404 if missing)."""
    try:
        return attempt_dict(service.cancel(s, attempt_id))
    except service.ApplyError as e:
        raise HTTPException(404, str(e)) from e


# ---- answers bank ---------------------------------------------------------------------


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300, pattern=r"^(q:[a-z0-9' ]+|[a-z_]+)$")
    value: str = Field(max_length=5000)


@router.get("/answers")
def get_answers(s: SessionDep, _p: UserDep):
    """Return the saved answer bank and the list of standard answer keys."""
    return {"answers": service.answers(s), "standard_keys": list(STANDARD_KEYS)}


@router.put("/answers")
def put_answer(body: AnswerIn, s: SessionDep, p: UserDep):
    """Save or overwrite one answer in the answer bank."""
    service.set_answer(s, body.key, body.value, source=p.label)
    return {"ok": True}


@router.delete("/answers/{key}")
def delete_answer(key: str, s: SessionDep, _p: UserDep):
    """Delete one saved answer (404 if missing)."""
    row = s.get(db.Answer, key)
    if not row:
        raise HTTPException(404, "not found")
    s.delete(row)
    s.commit()
    return {"ok": True}


# ---- screenshots (re-encoded PNGs only) -----------------------------------------------


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: int, s: SessionDep, _p: UserDep):
    """Serve a stored screenshot, only if the file lives in appa's own upload directory."""
    row = s.get(db.Upload, upload_id)
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row.path).resolve()
    if path.parent != uploads.upload_dir().resolve() or not path.is_file():
        raise HTTPException(404, "not found")  # only files we wrote, from our own directory
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Content-Disposition": "inline", "Cache-Control": "private, max-age=3600"},
    )
