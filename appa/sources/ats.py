"""Fetch postings from public ATS job-board APIs (Greenhouse, Lever, Ashby).

These are the same unauthenticated JSON feeds that power each company's
careers page, so no scraping or login is involved.
"""

from __future__ import annotations

from urllib.parse import quote

from appa.core.ats_urls import AtsRef, parse_url  # noqa: F401  (re-exported for callers)
from appa.core.models import Job
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden


def _seg(s: str) -> str:
    """Slugs and IDs come from untrusted links; keep them a single path segment."""
    return quote(s, safe="")


# ---- Greenhouse -------------------------------------------------------------


def _gh_job(slug: str, j: dict) -> Job:
    """A Job from a Greenhouse API posting, with hidden text marked."""
    text, hidden = html_to_text(j.get("content") or "")
    return Job(
        company=j.get("company_name") or slug,
        title=j["title"].strip(),
        url=j["absolute_url"],
        location=(j.get("location") or {}).get("name", ""),
        description=mark_hidden(text, hidden),
        ats="greenhouse",
        ats_job_id=str(j["id"]),
    )


def greenhouse_board(slug: str) -> list[Job]:
    """Every open posting on a Greenhouse board, with descriptions."""
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{_seg(slug)}/jobs", {"content": "true"})
    return [_gh_job(slug, j) for j in data.get("jobs", [])]


def greenhouse_job(slug: str, job_id: str) -> Job:
    """One Greenhouse posting by id."""
    return _gh_job(slug, get_json(f"https://boards-api.greenhouse.io/v1/boards/{_seg(slug)}/jobs/{_seg(job_id)}"))


# ---- Lever ------------------------------------------------------------------


def _lever_job(slug: str, j: dict) -> Job:
    """A Job from a Lever API posting: description, lists and additional text, with hidden text marked."""
    parts = [j.get("descriptionPlain") or ""]
    for lst in j.get("lists", []):
        items, _ = html_to_text(lst.get("content", ""))
        parts.append(f"{lst.get('text', '')}\n{items}")
    parts.append(j.get("additionalPlain") or "")
    hidden: list[str] = []
    for field in ("description", "additional"):
        hidden += html_to_text(j.get(field) or "")[1]
    return Job(
        company=slug,
        title=j["text"].strip(),
        url=j["hostedUrl"],
        location=(j.get("categories") or {}).get("location", ""),
        description=mark_hidden("\n\n".join(p for p in parts if p), hidden),
        ats="lever",
        ats_job_id=j["id"],
    )


def lever_board(slug: str) -> list[Job]:
    """Every open posting on a Lever board."""
    return [_lever_job(slug, j) for j in get_json(f"https://api.lever.co/v0/postings/{_seg(slug)}", {"mode": "json"})]


def lever_job(slug: str, job_id: str) -> Job:
    """One Lever posting by id."""
    return _lever_job(slug, get_json(f"https://api.lever.co/v0/postings/{_seg(slug)}/{_seg(job_id)}"))


# ---- Ashby ------------------------------------------------------------------


def _ashby_job(slug: str, j: dict) -> Job:
    """A Job from an Ashby API posting, with hidden text marked."""
    _, hidden = html_to_text(j.get("descriptionHtml") or "")
    return Job(
        company=slug,
        title=j["title"].strip(),
        url=j["jobUrl"],
        location=j.get("location", ""),
        description=mark_hidden(j.get("descriptionPlain") or "", hidden),
        ats="ashby",
        ats_job_id=j["id"],
    )


def ashby_board(slug: str) -> list[Job]:
    """Every listed posting on an Ashby board."""
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{_seg(slug)}")
    return [_ashby_job(slug, j) for j in data.get("jobs", []) if j.get("isListed", True)]


def ashby_job(slug: str, job_id: str) -> Job:
    """One Ashby posting, found by scanning its board; LookupError if it isn't there."""
    for job in ashby_board(slug):  # Ashby has no single-job endpoint
        if job.ats_job_id == job_id:
            return job
    raise LookupError(f"Ashby job {job_id} not found on board {slug} (may be closed)")


# ---- dispatch ---------------------------------------------------------------

BOARDS = {"greenhouse": greenhouse_board, "lever": lever_board, "ashby": ashby_board}
SINGLE = {"greenhouse": greenhouse_job, "lever": lever_job, "ashby": ashby_job}


def fetch_board(ats: str, slug: str) -> list[Job]:
    """All postings on a board of the given ATS."""
    return BOARDS[ats](slug)


def fetch_job(ref: AtsRef) -> Job:
    """Fetch the single posting an ATS ref points at; ValueError if it is a board, not a posting."""
    if not ref.job_id:
        raise ValueError("URL points at a job board, not a single posting")
    return SINGLE[ref.ats](ref.slug, ref.job_id)
