"""Misses and failures, recorded so you can see what to teach appa next.

Each row is one thing that didn't work automatically: a link with no job ID pattern, a form
question no standard answer covers, a field that didn't fill, a site that was handed back,
a submit without a confirmation page, an extraction error. `appa insights` groups them by
kind, site and subject, so the most common misses (the next regex, label pattern or site
fix worth writing) come first.

Rows hold labels, links, hosts and error messages only. Your answers are never stored here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from appa import db

# kind -> what to do about it (shown by `appa insights`)
KINDS = {
    "id_not_in_link": "no job ID pattern matched this link: add one to core/jobid.py if the ID is in it",
    "label_unmapped": "a form question no standard answer covers: add a pattern to core/apply_fields.py",
    "label_by_model": "the model picked the answer key: a pattern would make this free and certain",
    "fill_failed": "a field didn't fill: check runner/dom.py for this field type on this site",
    "blocked": "handed back to you: login, bot wall, off-site or no form found",
    "no_confirmation": "submitted but no confirmation seen: add this site's confirmation text",
    "submit_failed": "the submit step failed",
    "runner_error": "the runner gave up on a job after retries",
    "extract_error": "the model couldn't read a posting",
    "feed_error": "a job source failed: check its adapter in appa/feeds (endpoint or format may have changed)",
    "board_dead": "a company board kept failing and was switched off (moved ATS, renamed, or closed)",
    "describe_failed": "couldn't get a posting's full text before screening",
}


def _site(url: str | None) -> str:
    """The host of a link, without a leading www."""
    host = (urlparse(url or "").hostname or "").lower()
    return host.removeprefix("www.")[:255]


def path_shape(url: str | None) -> str:
    """A link's path with digits and long hex runs replaced, so similar links group together."""
    path = urlparse(url or "").path or "/"
    path = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{uuid}", path, flags=re.I)
    return re.sub(r"\d+", "{n}", path)[:300]


def record(
    s: Session,
    kind: str,
    subject: str,
    *,
    url: str | None = None,
    site: str | None = None,
    detail: dict | None = None,
    application_id: int | None = None,
    attempt_id: int | None = None,
) -> None:
    """Add one observation to the session (the caller commits with its own work)."""
    if kind not in KINDS:
        raise ValueError(f"unknown observation kind: {kind}")
    clean = {str(k)[:40]: str(v)[:500] for k, v in list((detail or {}).items())[:12] if v is not None}
    s.add(
        db.Observation(
            kind=kind,
            site=site or _site(url),
            subject=" ".join(str(subject).split())[:500],
            detail=clean,
            application_id=application_id,
            attempt_id=attempt_id,
        )
    )


def summary(s: Session, days: int = 30, kind: str | None = None, limit: int = 40) -> list[dict]:
    """The most frequent misses in the last `days`, grouped by kind, site and subject."""
    since = datetime.now(UTC) - timedelta(days=days)
    o = db.Observation
    q = (
        select(
            o.kind,
            o.site,
            o.subject,
            func.count().label("n"),
            func.max(o.created_at).label("last"),
            func.max(o.id).label("example_id"),
        )
        .where(o.created_at >= since)
        .group_by(o.kind, o.site, o.subject)
        .order_by(func.count().desc(), func.max(o.created_at).desc())
        .limit(limit)
    )
    if kind:
        q = q.where(o.kind == kind)
    return [dict(r._mapping) for r in s.execute(q)]


def counts(s: Session, days: int = 30) -> dict[str, int]:
    """How many observations of each kind in the last `days`."""
    since = datetime.now(UTC) - timedelta(days=days)
    o = db.Observation
    rows = s.execute(select(o.kind, func.count()).where(o.created_at >= since).group_by(o.kind))
    return {k: n for k, n in rows}


def prune(s: Session, older_than_days: int) -> int:
    """Delete observations older than this many days; returns how many were removed."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    n = s.execute(delete(db.Observation).where(db.Observation.created_at < cutoff)).rowcount
    s.commit()
    return n


def backfill(s: Session) -> int:
    """Seed observations from postings and attempts recorded before this table existed (once)."""
    from appa.core.apply_fields import FormQuestion, standard_key
    from appa.core.jobid import job_key

    if s.scalar(select(func.count()).select_from(db.Observation)):
        return 0
    n = 0
    for j in s.scalars(select(db.SeenJob)):
        if (j.url or "").startswith(("http://", "https://")) and not job_key(j.url):
            record(
                s,
                "id_not_in_link",
                path_shape(j.url),
                url=j.url,
                application_id=j.application_id,
                detail={"url": j.url, "id_source": "unknown (backfill)"},
            )
            n += 1
    for a in s.scalars(select(db.ApplyAttempt)):
        ids = {"url": a.apply_url, "attempt_id": a.id, "application_id": a.application_id}
        if (a.error or "").startswith("apply yourself: "):
            record(s, "blocked", a.error.removeprefix("apply yourself: ")[:200], **ids)
            n += 1
        kinds = {q["ref"]: q.get("kind") for q in a.form or []}
        for r in a.fill_report or []:
            if not r.get("ok"):
                record(
                    s,
                    "fill_failed",
                    r.get("label") or r.get("ref") or "?",
                    **ids,
                    detail={"kind": kinds.get(r.get("ref")), "error": r.get("error"), "ref": r.get("ref")},
                )
                n += 1
        for q in a.form or []:
            fq = FormQuestion.model_validate(q)
            item = next((i for i in a.plan or [] if i.get("ref") == fq.ref), None)
            if fq.kind != "textarea" and standard_key(fq) is None and (item is None or item.get("source") == "saved"):
                record(s, "label_unmapped", fq.label, **ids, detail={"kind": fq.kind, "reason": "backfill"})
                n += 1
    s.commit()
    return n
