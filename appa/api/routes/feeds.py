"""The listings inbox and job sources: what appa found, what it filtered and why, and your overrides."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from appa import db, feeds
from appa.api.deps import Principal, SessionDep, require
from appa.feeds import store

router = APIRouter(tags=["feeds"])
ReadDep = Annotated[Principal, Depends(require("applications:read"))]
WriteDep = Annotated[Principal, Depends(require("applications:write"))]
Status = Literal[db.LISTING_STATUSES]  # type: ignore[valid-type]


def listing_dict(r: db.FeedListing) -> dict:
    """A listing as JSON (stored posting text is not included)."""
    return {
        "id": r.id,
        "company": r.company,
        "title": r.title,
        "location": r.location,
        "url": r.url,
        "sources": r.sources,
        "posted_at": r.posted_at,
        "status": r.status,
        "reason": r.reason,
        "hints": r.hints,
        "application_id": r.application_id,
        "first_seen_at": r.first_seen_at,
    }


@router.get("/feeds")
def sources(s: SessionDep, _p: ReadDep):
    """Every job source, whether it's ready (keys set), and how many company boards are in rotation."""
    lists = [
        {
            "name": f.name,
            "description": f.description,
            "default": f.default,
            "terms": f.terms,
            "ready": all(os.environ.get(v) for v in (f.key_env, *f.extra_env) if v),
            "needs": [v for v in (f.key_env, *f.extra_env) if v],
        }
        for f in sorted(feeds.lists_registry().values(), key=lambda f: f.name)
    ]
    boards = dict(
        s.execute(select(db.Board.ats, func.count()).where(db.Board.enabled.is_(True)).group_by(db.Board.ats)).all()
    )
    inbox = dict(s.execute(select(db.FeedListing.status, func.count()).group_by(db.FeedListing.status)).all())
    return {
        "lists": lists,
        "board_ats": sorted(feeds.boards_registry()),
        "boards": boards,
        "inbox": inbox,
        "load_errors": sorted(feeds.LOAD_ERRORS),
    }


@router.get("/listings")
def list_(
    s: SessionDep,
    _p: ReadDep,
    status: Status | None = None,
    source: str | None = Query(None, max_length=64, pattern=r"^[a-z0-9_:]+$"),
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100_000),
):
    """Listings in the inbox, newest postings first; filter by status, source or company/title text."""
    return {
        "listings": [
            listing_dict(r) for r in store.search(s, status=status, source=source, q=q, limit=limit, offset=offset)
        ]
    }


def _get(s, listing_id: int) -> db.FeedListing:
    """The listing, or 404."""
    row = s.get(db.FeedListing, listing_id)
    if not row:
        raise HTTPException(404, "no such listing")
    return row


@router.post("/listings/{listing_id}/track", status_code=201)
def track(listing_id: int, s: SessionDep, p: WriteDep):
    """Add this listing to your applications as To Apply, without screening (you picked it)."""
    row = _get(s, listing_id)
    existing = s.scalars(select(db.Application).where(db.Application.job_key == row.key)).first()
    if existing:
        if row.application_id != existing.id:
            store.mark(s, row, "duplicate", f"already tracked ({existing.status})", existing.id)
        return {"application_id": existing.id, "created": False}
    x = store.as_listing(row)
    job = x.to_job("listing")
    app = db.Application(
        company=row.company,
        role=row.title,
        location=row.location or None,
        link=row.url,
        status="To Apply",
        source=f"listing:{(row.sources or ['?'])[0]}"[:64],
        job_id=job.posting_id,
        job_id_source=job.posting_id_source,
        notes=f"picked from the inbox by {p.label} (not screened)",
    )
    s.add(app)
    s.flush()
    store.mark(s, row, "screened", f"tracked by {p.label}", app.id)
    return {"application_id": app.id, "created": True}


@router.post("/listings/{listing_id}/skip")
def skip(listing_id: int, s: SessionDep, p: WriteDep):
    """Don't screen or apply to this listing."""
    row = _get(s, listing_id)
    store.mark(s, row, "skipped", f"skipped by {p.label}")
    return listing_dict(row)


@router.post("/listings/{listing_id}/restore")
def restore(listing_id: int, s: SessionDep, _p: WriteDep):
    """Put a filtered, skipped or failed listing back in line for screening."""
    row = _get(s, listing_id)
    if row.status not in ("filtered", "skipped", "error"):
        raise HTTPException(422, f"listing is {row.status}; only filtered, skipped or failed ones can be restored")
    store.mark(s, row, "new", None)
    return listing_dict(row)
