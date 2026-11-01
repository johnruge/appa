"""SmartRecruiters job boards, through the public Posting API (no key needed for public postings).

GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=N lists a company's
public postings (an unknown company is just an empty list); the list has no text, so describe() fetches
one posting's job ad from .../postings/{id}. Boards are the {company} in jobs.smartrecruiters.com/{company}/...
and careers.smartrecruiters.com/{company} links. Postings are linked to their public page on
jobs.smartrecruiters.com, which is also where you apply.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources import http
from appa.sources.text import html_to_text, mark_hidden

API = "https://api.smartrecruiters.com/v1/companies"
JOBS = "https://jobs.smartrecruiters.com"
PAGE = 100
MAX_PAGES = 50
PAUSE = 0.5
MAX_TEXT = 100_000
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
POSTING_ID = re.compile(r"^\d{1,30}$")
# First path segments on jobs.smartrecruiters.com that are site pages, not companies.
NOT_COMPANIES = {
    "oneclick-ui",
    "external-referrals",
    "sr-jobs",
    "app",
    "static",
    "api",
    "public",
    "search",
    "robots.txt",
    "favicon.ico",
    "sitemap.xml",
    "ni",
    "en",
    "jobs",
    "login",
    "error",
}
LEVELS = {
    "internship": "intern",
    "entry_level": "entry",
    "mid_senior_level": "mid",
    "director": "senior",
    "executive": "senior",
}


def _slug(slug: str) -> str:
    """The company identifier, or ValueError if it isn't a plain SmartRecruiters identifier."""
    if not isinstance(slug, str) or not SLUG.match(slug):
        raise ValueError(f"not a SmartRecruiters company identifier: {slug!r}"[:200])
    return slug


def _date(s) -> date | None:
    """The date part of an ISO timestamp like 2026-10-30T21:22:47.319Z."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _employment(label: str) -> str:
    """appa's employment type for the source's label (labels are set per company)."""
    low = label.lower()
    for word, kind in (
        ("intern", "internship"),
        ("part", "part_time"),
        ("contract", "contract"),
        ("temp", "contract"),
        ("full", "full_time"),
        ("perman", "full_time"),
    ):
        if word in low:
            return kind
    return label


def _url(company: str, pid: str, name: str) -> str:
    """The public posting page: jobs.smartrecruiters.com/{company}/{id}-{title-slug}."""
    tail = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{JOBS}/{quote(company, safe='')}/{quote(pid, safe='')}" + (f"-{tail}" if tail else "")


def _listing(company: str, p: dict) -> Listing:
    """One posting from the list endpoint (ValueError if it's malformed)."""
    pid = str(p.get("id") or "")
    if not POSTING_ID.match(pid):
        raise ValueError("posting has no id")
    loc = p.get("location") or {}
    level = (p.get("experienceLevel") or {}).get("id") or ""
    kind = (p.get("typeOfEmployment") or {}).get("label") or ""
    hints = {
        "level": LEVELS.get(level, ""),
        "employment_type": _employment(kind) if kind else "",
        "remote": "yes" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "no" if loc else "",
        "country": str(loc.get("country") or "").upper(),
        "category": (p.get("function") or {}).get("label") or "",
    }
    place = loc.get("fullLocation") or ", ".join(
        str(x) for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
    )
    return Listing(
        company=(p.get("company") or {}).get("name") or company,
        title=str(p.get("name") or ""),
        url=_url(company, pid, str(p.get("name") or "")),
        location=place,
        posted_at=_date(p.get("releasedDate")),
        ats="smartrecruiters",
        ats_job_id=pid,
        source_ref=p.get("uuid") or pid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open public posting on this company's SmartRecruiters board (no descriptions; see describe)."""
    company, opts = _slug(slug), opts or FeedOptions()
    params: dict = {"limit": PAGE}
    if opts.us_only:
        params["country"] = "us"
    if opts.since:
        params["releasedAfter"] = opts.since.isoformat()
    out: list[Listing] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        data = http.get_json(f"{API}/{quote(company, safe='')}/postings", params={**params, "offset": page * PAGE})
        rows = data.get("content") if isinstance(data, dict) else None
        if not rows:
            break
        for p in rows:
            try:
                x = _listing(company, p if isinstance(p, dict) else {})
            except ValueError:
                continue
            if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if len(rows) < PAGE or (page + 1) * PAGE >= int(data.get("totalFound") or 0):
            break
    return out


def describe(listing: Listing) -> str:
    """The posting's job ad (company, job description, qualifications, more info) as text."""
    company, pid = board_of(listing.url), str(listing.ats_job_id or "")
    if not company or not POSTING_ID.match(pid):
        raise ValueError("not a SmartRecruiters posting")
    data = http.get_json(f"{API}/{quote(company, safe='')}/postings/{quote(pid, safe='')}")
    sections = ((data.get("jobAd") or {}).get("sections") or {}) if isinstance(data, dict) else {}
    parts, hidden = [], []
    for s in sections.values():
        if not isinstance(s, dict) or not s.get("text"):
            continue
        text, h = html_to_text(str(s["text"]))
        parts.append(f"{s.get('title') or ''}\n{text}".strip())
        hidden += h
    return mark_hidden("\n\n".join(parts), hidden)[:MAX_TEXT]


def board_of(url: str) -> str | None:
    """The company identifier in a SmartRecruiters link (jobs./careers.smartrecruiters.com), else None."""
    try:
        p = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme not in ("http", "https"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if (
        host == "jobs.smartrecruiters.com"
        and len(parts) >= 3
        and parts[0] in ("oneclick-ui", "external-referrals")
        and parts[1] == "company"
    ):
        cand = parts[2]
    elif host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        cand = parts[0]
    elif host == "api.smartrecruiters.com" and len(parts) >= 3 and parts[:2] == ["v1", "companies"]:
        cand = parts[2]
    else:
        return None
    if cand.lower() in NOT_COMPANIES or not SLUG.match(cand):
        return None
    return cand


BOARD = register_board(
    BoardFeed(
        ats="smartrecruiters",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="BoschGroup",
        terms="Public Posting API (built for customers' career sites; public postings need no key). "
        "api.smartrecruiters.com/robots.txt disallows crawlers, so keep it to paced, personal polling.",
        hosts=("api.smartrecruiters.com",),
    )
)
