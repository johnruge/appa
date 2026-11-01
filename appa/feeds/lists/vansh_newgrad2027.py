"""vanshb03/New-Grad-2027 (formerly cvrve/New-Grad and New-Grad-2026): a curated GitHub list of
entry-level SWE, data, quant and PM roles for 2026/2027 graduates in the US, Canada and remote. Same
listings.json schema as SimplifyJobs (MIT licensed), read straight from raw.githubusercontent.com.

Every row's url is a go.vansh.nyc shortlink that 302-redirects to the real ATS page. go.vansh.nyc's
robots.txt disallows all automated access (and counts clicks), so appa does NOT resolve the links in
bulk: the shortlink is the Listing url, the row's uuid is source_ref, and there's no ats_job_id (so
these rows won't dedupe against the same job from other sources). The pipeline's page fetch opens a
shortlink once, only for a posting that passes the free filters. Every active, visible row is kept;
date filtering happens only when opts.since is set.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/vanshb03/New-Grad-2027/dev/.github/scripts/listings.json"


def _date(ts) -> date | None:
    """A unix timestamp from the file, as a UTC date (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _listing(row) -> Listing | None:
    """A Listing from one row of the file, or None for rows that are closed, hidden or malformed."""
    if not isinstance(row, dict) or not row.get("active") or not row.get("is_visible", True):
        return None
    url, company, title = str(row.get("url") or ""), str(row.get("company_name") or ""), str(row.get("title") or "")
    if not (url.startswith(("http://", "https://")) and company.strip() and title.strip()):
        return None
    sponsorship = str(row.get("sponsorship") or "")
    hints = {"level": "new_grad", "employment_type": "full_time"}
    if sponsorship and sponsorship != "Other":  # "Other" means the list didn't label it
        hints["sponsorship"] = sponsorship
    if "citizenship" in sponsorship.lower():
        hints["citizenship_required"] = "yes"
    locations = [str(x) for x in row.get("locations") or [] if isinstance(x, (str, int, float))]
    if any("remote" in x.lower() for x in locations):
        hints["remote"] = "yes"
    if locations and all("canada" in x.lower() for x in locations):
        hints["country"] = "Canada"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location="; ".join(locations)[:500],
            posted_at=_date(row.get("date_posted")),
            source_ref=str(row.get("id") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every active, visible listing (newest first), optionally only those posted since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("New-Grad-2027 listings file is not a list")
    out = [x for x in map(_listing, rows) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="vansh_newgrad2027",
        description="vanshb03/New-Grad-2027 (GitHub): curated 2026/2027 new-grad SWE/data/quant/PM roles, "
        "US/Canada/remote",
        fetch=fetch,
        terms="MIT-licensed public GitHub repo; links are go.vansh.nyc shortlinks whose robots.txt disallows "
        "automated access, so appa doesn't resolve them in bulk; a shortlink is opened once per "
        "posting, only after it passes the free filters (about one human click).",
    )
)
