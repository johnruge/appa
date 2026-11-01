"""Remotive: a remote-only job board with a free public JSON API (no key). One GET returns every active
listing with its full HTML description, so appa makes a single request per run and filters locally
(the category/search parameters only narrow the same set, and Remotive blocks more than 2 requests a
minute and asks for at most 4 calls a day). Listings link back to the Remotive posting page, as the
API terms require. The public API is delayed 24 hours and currently returns only a few dozen jobs.
"""

from __future__ import annotations

import re
from datetime import datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://remotive.com/api/remote-jobs"
MAX_DESCRIPTION = 100_000

# Remotive categories that can hold software/new-grad roles; the rest (sales, writing...) are skipped.
TECH_CATEGORIES = {
    "software development",
    "artificial intelligence",
    "data and analytics",
    "devops",
    "quality assurance",
    "information technology",
    "engineering",
    "product management",
    "research",
}
# Location words that let a US-based candidate apply.
_US_OK = re.compile(r"\b(usa|us|u\.s\.|united states|north america|americas|worldwide|anywhere|global)\b", re.I)


def _date(value) -> datetime | None:
    """The API's ISO publication timestamp as a datetime (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _us_ok(location: str) -> bool:
    """True when a remote location restriction is empty or admits candidates in the US."""
    return not location.strip() or bool(_US_OK.search(location))


def _listing(row: dict) -> Listing | None:
    """A Listing from one job of the API response, or None for malformed rows."""
    if not isinstance(row, dict):
        return None
    url, company, title = str(row.get("url") or ""), str(row.get("company_name") or ""), str(row.get("title") or "")
    job_id = str(row.get("id") or "")
    if not (url.startswith("https://remotive.com/") and company.strip() and title.strip() and job_id):
        return None
    text, hidden = html_to_text(str(row.get("description") or ""))
    location = str(row.get("candidate_required_location") or "")
    posted = _date(row.get("publication_date"))
    hints = {
        "remote": "yes",
        "country": location,
        "category": str(row.get("category") or ""),
        "salary": str(row.get("salary") or ""),
        "employment_type": str(row.get("job_type") or ""),
    }
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=f"Remote ({location})" if location else "Remote",
            posted_at=posted.date() if posted else None,
            description=mark_hidden(text, hidden)[:MAX_DESCRIPTION],
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every active tech listing (newest first), optionally only those posted since opts.since or open to the US."""
    data = get_json(URL)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Remotive response has no jobs list")
    out, seen = [], set()
    for x in map(_listing, jobs):
        if x is None or x.ats_job_id in seen or x.hints.get("category", "").lower() not in TECH_CATEGORIES:
            continue
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        if opts.us_only and not _us_ok(x.hints.get("country", "")):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
    out.sort(key=lambda x: x.posted_at or datetime.min.date(), reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="remotive",
        description="Remotive public API: remote-only tech jobs with full descriptions (24h delayed, small set)",
        fetch=fetch,
        terms="Free public API, no key. Max 2 requests/minute (excess is blocked); they advise at most 4 calls a "
        "day. Link back to the Remotive URL and credit Remotive; never resubmit the jobs to other job boards.",
    )
)
