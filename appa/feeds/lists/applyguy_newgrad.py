"""ApplyGuy 2027-New-Grad-Jobs: a GitHub list of (mostly U.S.) new-grad and entry-level software roles posted in
the last 60 days, refreshed about every 15 minutes by Apply Guy from employer career pages. appa reads
the repo's machine-readable feed (data/new-grad-jobs.json, a few hundred rows). Each row links straight
to the employer's ATS posting (Greenhouse, Workday, Ashby, Lever, Paylocity...); the row's applyguy.ai
tracking link is ignored. The feed has no posting text, so the scout reads the ATS page later.
"""

from __future__ import annotations

from datetime import date

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/ApplyGuy/2027-New-Grad-Jobs/main/data/new-grad-jobs.json"
LEVELS = {"New Grad": "new_grad", "Entry Level": "entry"}


def _date(s) -> date | None:
    """A YYYY-MM-DD string from the feed as a date (None if missing or invalid)."""
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _ats(url: str) -> tuple[str, str | None]:
    """(ats, job id) read from the employer link: Greenhouse/Lever/Ashby by name, Workday by host, else unknown."""
    ref = parse_url(url)
    if ref and ref.job_id:
        return ref.ats, ref.job_id[:100]
    key = job_key(url)
    jid = key.split(":", 1)[1][:100] if key else None
    return ("workday" if key and "myworkdayjobs.com:" in key else "unknown"), jid


def _listing(row: dict) -> Listing | None:
    """A Listing from one feed row, or None when the row has no direct employer link or is malformed."""
    if not isinstance(row, dict):
        return None
    url = str(row.get("listingUrl") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    location = str(row.get("location") or "")
    hints = {"level": LEVELS.get(str(row.get("eligibility") or ""), ""), "category": "Software"}
    if "remote" in location.lower():
        hints["remote"] = "yes"
    ats, jid = _ats(url)
    try:
        return Listing(
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            url=url,
            location=location[:500],
            posted_at=_date(row.get("posted")),
            ats=ats,
            ats_job_id=jid,
            source_ref=str(row.get("id") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every listing in the feed (newest first), optionally only those posted since opts.since."""
    data = get_json(URL)
    rows = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ApplyGuy new-grad feed has no jobs list")
    out = [x for x in map(_listing, rows) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="applyguy_newgrad",
        description="ApplyGuy 2027-New-Grad-Jobs (GitHub): mostly U.S. new-grad/entry-level SWE roles, last 60 days, "
        "~15 min refresh",
        fetch=fetch,
        terms="Public GitHub repository (no license file); its README invites machine-readable use of "
        "data/new-grad-jobs.json. Personal use; don't redistribute the data.",
    )
)
