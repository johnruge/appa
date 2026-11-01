"""Applications: your source of truth. Agents check before applying and record after."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from appa import db
from appa.api.deps import Principal, SessionDep, require

router = APIRouter(prefix="/applications", tags=["applications"])

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[A-Za-z]{2,}$")
Status = Literal[db.STATUSES]  # type: ignore[valid-type]


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: str | None = Field(None, max_length=100)
    link: str | None = Field(None, max_length=2000)
    # The posting's own ID when the link doesn't carry one (requisition number, posting ID).
    job_id: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")
    location: str | None = Field(None, max_length=300)
    date_applied: date | None = None
    applied_with_email: str | None = Field(None, max_length=254)
    referral: bool | None = None
    people: str | None = Field(None, max_length=500)
    todo: str | None = Field(None, max_length=5000)
    notes: str | None = Field(None, max_length=10000)

    @field_validator("link")
    @classmethod
    def _link(cls, v):
        """Accept only http(s) URLs; an empty link becomes None."""
        if v and not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("must be an http(s) URL")
        return v or None

    @field_validator("applied_with_email")
    @classmethod
    def _email(cls, v):
        """Accept only valid email addresses; an empty one becomes None."""
        if v and not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v or None


class ApplicationIn(_Fields):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=300)
    status: Status = "Applied"


class ApplicationPatch(_Fields):
    company: str | None = Field(None, min_length=1, max_length=200)
    role: str | None = Field(None, min_length=1, max_length=300)
    status: Status | None = None


@router.get("/check")
def check(
    s: SessionDep,
    _p: Annotated[Principal, Depends(require("applications:read"))],
    company: str = Query(min_length=1, max_length=200),
    role: str = Query("", max_length=300),
    link: str = Query("", max_length=2000),
    job_id: str = Query("", max_length=100),
):
    """Ask before applying. Only `already_applied: true` (same job ID) means don't apply;
    the company's other rows are context, never a reason to skip."""
    m = db.find_match(s, company, role, link, job_id or None)
    if not m:
        return {"already_applied": False, "match": None, "application": None, "other_roles_at_company": []}
    same = m.kind == "same_posting"
    return {
        "already_applied": same,
        "match": m.kind,
        "application": m.app.to_dict() if same else None,
        "other_roles_at_company": [a.to_dict() for a in m.others],
    }


@router.post("", status_code=201)
def create(
    body: ApplicationIn,
    s: SessionDep,
    p: Annotated[Principal, Depends(require("applications:write"))],
):
    """Record an application. Returns 409 with the existing record only if the job ID
    in `link` is already tracked. Without a job ID it's always recorded: a duplicate
    beats a missing application."""
    m = db.find_match(s, body.company, body.role, body.link, body.job_id)
    if m and m.kind == "same_posting":
        return _conflict(m.kind, m.app)
    a = db.Application(**body.model_dump(), source=p.label, job_id_source="provided" if body.job_id else None)
    if a.status in db.APPLIED_STATUSES and not a.date_applied:
        a.date_applied = date.today()
    s.add(a)
    try:
        s.commit()
    except IntegrityError:  # another request recorded the same job ID first
        s.rollback()
        return _conflict("same_posting", db.find_match(s, body.company, body.role, body.link, body.job_id).app)
    return a.to_dict()


def _conflict(kind: str, existing: db.Application) -> JSONResponse:
    """A 409 response saying the posting is already tracked, with the matching application."""
    return JSONResponse(
        {"error": "already tracked", "match": kind, "application": existing.to_dict()},
        status_code=status.HTTP_409_CONFLICT,
    )


@router.patch("/{app_id}")
def update(
    app_id: int,
    body: ApplicationPatch,
    s: SessionDep,
    _p: Annotated[Principal, Depends(require("applications:write"))],
):
    """Update fields of an application; a link whose job ID belongs to another application returns 409."""
    a = s.get(db.Application, app_id)
    if not a:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    try:
        s.commit()
    except IntegrityError:  # new link's job ID belongs to another application
        s.rollback()
        other = db.find_match(s, a.company, a.role, body.link)
        if other and other.kind == "same_posting":
            return _conflict("same_posting", other.app)
        raise
    return a.to_dict()


@router.get("/{app_id}")
def get_one(app_id: int, s: SessionDep, _p: Annotated[Principal, Depends(require("applications:read"))]):
    """Return one application by id (404 if missing)."""
    a = s.get(db.Application, app_id)
    if not a:
        raise HTTPException(404, "not found")
    out = a.to_dict()
    seen = s.scalars(select(db.SeenJob).where(db.SeenJob.application_id == a.id)).first()
    ex = (seen.extraction or {}) if seen else {}
    out["screening"] = (
        {
            "verdict": seen.verdict,
            "reasons": seen.reasons or [],
            "source": seen.source,
            "sponsorship": ex.get("sponsorship"),
            "sponsorship_quote": ex.get("sponsorship_quote"),
            "min_years_experience": ex.get("min_years_experience"),
            "experience_quote": ex.get("experience_quote"),
            "is_entry_level": ex.get("is_entry_level"),
            "seniority": ex.get("seniority"),
            "citizenship_required": ex.get("citizenship_required"),
            "clearance_required": ex.get("clearance_required"),
        }
        if seen
        else None
    )
    attempt = s.scalars(
        select(db.ApplyAttempt).where(db.ApplyAttempt.application_id == a.id).order_by(db.ApplyAttempt.id.desc())
    ).first()
    out["prefill"] = {"id": attempt.id, "status": attempt.status, "error": attempt.error} if attempt else None
    return out


@router.get("")
def list_(
    s: SessionDep,
    _p: Annotated[Principal, Depends(require("applications:read"))],
    company: str | None = Query(None, max_length=200),
    status_: list[Status] | None = Query(None, alias="status", description="one or more statuses"),
    q: str | None = Query(None, max_length=200, description="search company, role, notes"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List applications, newest first, filtered by company, status or a text search, with paging."""
    stmt = select(db.Application)
    if company:
        stmt = stmt.where(db.Application.company_norm == db.normalize.company(company))
    if status_:
        stmt = stmt.where(db.Application.status.in_(status_))
    if q:
        term = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{term}%"  # bound parameter; your wildcards are escaped
        stmt = stmt.where(
            or_(
                db.Application.company.ilike(like, escape="\\"),
                db.Application.role.ilike(like, escape="\\"),
                db.Application.notes.ilike(like, escape="\\"),
            )
        )
    rows = s.scalars(stmt.order_by(db.Application.id.desc()).limit(limit).offset(offset))
    return {"applications": [a.to_dict() for a in rows]}
