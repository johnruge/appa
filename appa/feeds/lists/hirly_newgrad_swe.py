"""hirly New Grad Software Engineer Jobs: a daily list of SWE postings whose title asks for a new grad,
entry-level or junior engineer, read by hirly.me straight from company career pages (published on GitHub
as Hirly-Me/New-Grad-Software-Engineer-Jobs). appa reads the public JSON behind the README: the newest
250 of the ~1000 matching jobs, worldwide. Links are hirly.me job pages; describe() reads the posting
text from that page's schema.org JobPosting data.
"""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

URL = "https://hirly.me/api/job-lists/new-grad-software-engineer-jobs.json"
_JOB_PATH = re.compile(r"^/jobs/[a-z0-9-]{1,300}-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
MAX_DESCRIPTION = 100_000


def _job_url(url: str) -> tuple[str, str] | None:
    """(clean hirly job page URL, posting UUID) for a hirly.me job link, else None (tracking params dropped)."""
    p = urlparse(url.strip())
    m = _JOB_PATH.match(p.path)
    if p.scheme != "https" or (p.hostname or "").lower() != "hirly.me" or not m:
        return None
    return f"https://hirly.me{p.path}", m.group(1)


def _date(s) -> date | None:
    """A YYYY-MM-DD string as a date (None if missing or invalid)."""
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _listing(row: dict) -> Listing | None:
    """A Listing from one job in the file, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    job = _job_url(str(row.get("url") or ""))
    if not job:
        return None
    hints = {"level": "new_grad", "country": str(row.get("country") or "")}
    if row.get("work_mode") in ("remote", "hybrid"):
        hints["remote"] = "yes" if row["work_mode"] == "remote" else "hybrid"
    location = str(row.get("location") or "")
    more = row.get("more_locations")
    if isinstance(more, int) and more > 0:
        location = f"{location} (+{more} more)"
    try:
        return Listing(
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            url=job[0],
            location=location[:500],
            posted_at=_date(row.get("posted_on")),
            ats_job_id=job[1],
            source_ref=job[1],
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """The listed jobs (newest first): US or country-unknown ones when opts.us_only, posted since opts.since."""
    data = get_json(URL)
    rows = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("hirly job list has no jobs array")
    out, seen = [], set()
    for row in rows:
        x = _listing(row)
        if x is None or x.ats_job_id in seen:
            continue
        if opts.us_only and x.hints.get("country") not in (None, "US"):
            continue
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


def _job_posting(page: str) -> dict | None:
    """The schema.org JobPosting object embedded in a hirly job page, if any."""
    for tag in BeautifulSoup(page, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else data if isinstance(data, list) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def describe(listing: Listing) -> str:
    """The posting text from its hirly job page (one request); "" when the page has none."""
    job = _job_url(listing.url)
    if not job:
        return ""
    page, _ = get_text(job[0])
    posting = _job_posting(page) or {}
    text, hidden = html_to_text(str(posting.get("description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESCRIPTION]


FEED = register_list(
    ListFeed(
        name="hirly_newgrad_swe",
        description="hirly New Grad Software Engineer Jobs (hirly.me / GitHub): newest 250 new-grad/junior SWE "
        "roles, daily",
        fetch=fetch,
        describe=describe,
        terms="Public JSON (MIT-licensed list format; postings belong to employers). hirly's terms allow automation "
        "only for your own job search; no reselling or redistribution. Only the newest 250 jobs are exposed.",
    )
)
