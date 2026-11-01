"""Working Nomads: a remote-jobs board. Its public JSON feed (linked as "API" in the site footer,
robots.txt allows everything) returns the latest few dozen postings in one response, with the full
HTML description, company, category, tags, location text and publish time. There is no pagination
or search. appa keeps the Development category and treats every posting as remote.
"""

from __future__ import annotations

import re
from datetime import datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://www.workingnomads.com/api/exposed_jobs/"
CATEGORIES = {"development"}
_ID = re.compile(r"^https://www\.workingnomads\.com/job/go/(\d{1,20})/?$")
_HREF = re.compile(r"""href\s*=\s*["'](https?://[^"'\s<>]{1,500})["']""", re.I)


def _date(raw) -> datetime | None:
    """The feed's ISO publish time (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _listing(row: dict) -> Listing | None:
    """A Listing from one feed row, or None for other categories and malformed rows."""
    if not isinstance(row, dict) or str(row.get("category_name") or "").strip().lower() not in CATEGORIES:
        return None
    url = str(row.get("url") or "").strip()
    m = _ID.match(url)
    if not m:
        return None
    raw = str(row.get("description") or "")
    text, hidden = html_to_text(raw)
    links = list(dict.fromkeys(_HREF.findall(raw)))[:10]
    posted = _date(row.get("pub_date"))
    hints = {"category": str(row.get("category_name") or ""), "remote": "yes", "links": " ".join(links)}
    try:
        return Listing(
            company=str(row.get("company_name") or ""),
            title=str(row.get("title") or ""),
            url=url,
            location=str(row.get("location") or "")[:500],
            posted_at=posted.date() if posted else None,
            description=mark_hidden(text, hidden)[:100_000],
            ats_job_id=m.group(1),
            source_ref=m.group(1),
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """The feed's Development postings (one request), optionally only those posted since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("Working Nomads feed is not a list")
    out, seen = [], set()
    for x in map(_listing, rows):
        if x is None or x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="workingnomads",
        description="Working Nomads public JSON feed: latest remote Development postings (a few dozen, one request)",
        fetch=fetch,
        terms="Public feed linked from the site footer; robots.txt allows all. Personal use; no resale or "
        "competing product (site terms 3.1).",
    )
)
