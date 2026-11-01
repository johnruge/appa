"""SimplifyJobs New-Grad-Positions: a curated, daily-updated list of new-grad SWE, data, quant and
PM roles, maintained on GitHub by Simplify and contributors. appa reads the machine-readable
listings file behind the README (thousands of rows, about a quarter on Greenhouse/Lever/Ashby).
"""

from __future__ import annotations

from datetime import UTC, datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"


def _date(ts) -> datetime | None:
    """A unix timestamp from the file, as a UTC datetime (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _listing(row: dict) -> Listing | None:
    """A Listing from one row of the file, or None for rows that are closed, hidden or malformed."""
    if not row.get("active") or not row.get("is_visible", True):
        return None
    url, company, title = row.get("url") or "", row.get("company_name") or "", row.get("title") or ""
    if not (url.startswith(("http://", "https://")) and company.strip() and title.strip()):
        return None
    posted = _date(row.get("date_posted"))
    sponsorship = str(row.get("sponsorship") or "")
    hints = {
        "category": str(row.get("category") or ""),
        "degrees": ", ".join(str(d) for d in row.get("degrees") or []),
        "level": "new_grad",
        "employment_type": "full_time",
    }
    if sponsorship and sponsorship != "Other":  # "Other" means Simplify didn't label it
        hints["sponsorship"] = sponsorship
    if "citizenship" in sponsorship.lower():
        hints["citizenship_required"] = "yes"
    locations = [str(x) for x in row.get("locations") or []]
    if any("remote" in x.lower() for x in locations):
        hints["remote"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location="; ".join(locations)[:500],
            posted_at=posted.date() if posted else None,
            source_ref=str(row.get("id") or "") or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every active, visible listing (newest first), optionally only those posted since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("SimplifyJobs listings file is not a list")
    out = [x for x in map(_listing, rows) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or datetime.min.date(), reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="simplify_newgrad",
        description="SimplifyJobs New-Grad-Positions (GitHub): curated new-grad SWE/data/quant/PM roles, daily",
        fetch=fetch,
        terms="Public GitHub repository; personal use. Check its license before redistributing the data.",
    )
)
