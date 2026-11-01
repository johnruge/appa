"""The Muse public jobs API: a large US-heavy job board with a free JSON API (needs a registered app key).

appa asks for Entry Level postings in the software, data and IT categories (several `category`
params are OR'd by the API). Results are sorted by The Muse's own relevance mix, not by date, and the
API serves at most 100 pages of 20, so one run sees a slice of the matches, not all of them; posting
text comes inline as HTML in `contents`. The API Terms of Use (2.2) require registering a free app at
themuse.com/developers/api/v2/apps; its key is read from THEMUSE_API_KEY (3600 requests/hour) and the
source is skipped while it is unset.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

import httpx

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://www.themuse.com/api/public/jobs"
KEY_ENV = "THEMUSE_API_KEY"
# "Software Engineer", "Data Science", "Computer and IT" and "IT" returned 0 results in Oct 2026, so they're left out.
CATEGORIES = ("Software Engineering", "Data and Analytics", "Science and Engineering")
LEVEL = "Entry Level"
MAX_PAGES = 50  # the API itself refuses page >= 100
PAUSE = 0.5  # seconds between pages
MAX_DESCRIPTION = 100_000
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split()
)
_STATE = re.compile(r",\s*([A-Z]{2})$")  # case-sensitive "City, ST"
_US_WORDS = re.compile(r"united states|\busa?\b|remote|flexible", re.I)


def _date(ts) -> datetime | None:
    """An ISO publication_date ("2026-09-12T18:34:50Z") as a datetime (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _names(items) -> list[str]:
    """The "name" of each {"name": ...} entry in a list, skipping anything else."""
    return [str(x["name"]).strip() for x in items or [] if isinstance(x, dict) and x.get("name")]


def _is_us_one(location: str) -> bool:
    """True when one location names a US state ("City, ST"), the US, or remote/flexible work."""
    m = _STATE.search(location)
    return bool(m and m.group(1) in _US_STATES) or bool(_US_WORDS.search(location))


def _is_us(locations: list[str]) -> bool:
    """True when a posting has no location or any location looks US ("City, ST") or remote."""
    return not locations or any(_is_us_one(x) for x in locations)


def _listing(row: dict) -> Listing | None:
    """A Listing from one API result, or None when it's malformed."""
    if not isinstance(row, dict):
        return None
    url = str((row.get("refs") or {}).get("landing_page") or "")
    company = str((row.get("company") or {}).get("name") or "")
    title, job_id = str(row.get("name") or ""), row.get("id")
    if not (url.startswith("https://www.themuse.com/") and company.strip() and title.strip() and job_id):
        return None
    locations = _names(row.get("locations"))
    text, hidden = html_to_text(str(row.get("contents") or ""))
    posted = _date(row.get("publication_date"))
    levels = [str(x.get("short_name") or "") for x in row.get("levels") or [] if isinstance(x, dict)]
    hints = {
        "category": ", ".join(_names(row.get("categories"))),
        "level": "intern" if "internship" in levels else "entry" if "entry" in levels else "",
    }
    if any("remote" in x.lower() or "flexible" in x.lower() for x in locations):
        hints["remote"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location="; ".join(locations)[:500],
            posted_at=posted.date() if posted else None,
            description=mark_hidden(text, hidden)[:MAX_DESCRIPTION],
            ats_job_id=str(job_id),
            source_ref=str(job_id),
            hints=hints,
        )
    except ValueError:
        return None


def _page(page: int, key: str) -> dict:
    """One page of results; HTTP errors are re-raised without the URL so the API key never leaks."""
    params: dict = {"page": page, "level": LEVEL, "category": list(CATEGORIES), "api_key": key}
    try:
        data = get_json(URL, params=params)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"The Muse API returned HTTP {e.response.status_code} for page {page}") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"The Muse API request failed for page {page}: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise ValueError("The Muse API reply is not an object")
    return data


def fetch(opts: FeedOptions) -> list[Listing]:
    """Entry-level software/data/IT postings, page by page up to MAX_PAGES or opts.max_results."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    out: list[Listing] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        data = _page(page, key)
        rows = data.get("results") or []
        for x in map(_listing, rows):
            if x is None or x.ats_job_id in seen:
                continue
            if opts.since and x.posted_at and x.posted_at < opts.since:
                continue
            if opts.us_only and not _is_us([s.strip() for s in x.location.split(";") if s.strip()]):
                continue
            seen.add(x.ats_job_id)
            out.append(x)
        if not rows or len(out) >= opts.max_results or page + 1 >= int(data.get("page_count") or 0):
            break
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="themuse",
        description="The Muse public jobs API: Entry Level software/data/IT postings across US employers",
        fetch=fetch,
        key_env=KEY_ENV,
        terms=(
            "The Muse API Terms of Use 2.2 require registering a free app (themuse.com/developers/api/v2/apps) "
            "and using its key (THEMUSE_API_KEY, 3600 req/h). Every listing links back to its themuse.com landing "
            "page; descriptions are kept only for the owner's private screening and never republished (3.3(g) "
            "forbids cloning Muse Content). No web scraping of the site."
        ),
    )
)
