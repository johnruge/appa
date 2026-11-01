"""Microsoft Careers: every Microsoft opening, from the public search API behind apply.careers.microsoft.com
(an Eightfold "PCSX" site; robots.txt allows /api/pcsx and /careers). No login or key needed.

Search returns 10 postings a page, newest first, without the description; describe() fetches one
posting's text from the position_details endpoint. The API answers cookieless clients with frequent
HTTP 429s, so pages are paced ~2s apart and a 429 is retried with backoff; if a page still can't be read
after that, that query is cut short (logged as a warning) and fetch moves on to the next query after one
long wait. It raises only when nothing at all was read.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx  # only to catch HTTPStatusError; requests go through appa.sources.http

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

BASE = "https://apply.careers.microsoft.com"
SEARCH = f"{BASE}/api/pcsx/search"
DETAILS = f"{BASE}/api/pcsx/position_details"
PAGE = 10  # the API's fixed page size
MAX_PAGES = 50  # per query
PAUSE = 2.0  # seconds between requests
RETRY_PAUSES = (4.0, 8.0, 16.0)  # waits before each retry after a 429
COOLDOWN = 30.0  # seconds to wait before the next query after a persistent 429
REMOTE = {"onsite": "no", "remote": "yes", "hybrid": "hybrid"}


def _get(url: str, params: dict) -> dict:
    """One API call, retried with backoff while the API says 429 (too many requests)."""
    for wait in (*RETRY_PAUSES, None):
        try:
            return get_json(url, params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429 or wait is None:
                raise
            time.sleep(wait)


def _date(ts) -> datetime | None:
    """A unix timestamp from the API, as a UTC datetime (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _listing(row: dict) -> Listing | None:
    """A Listing from one search result, or None for a malformed row."""
    if not isinstance(row, dict):
        return None
    pid = str(row.get("id") or "")
    if not pid.isdigit():
        return None
    posted = _date(row.get("postedTs"))
    places = [str(x) for x in row.get("standardizedLocations") or [] if x]
    hints = {
        "category": str(row.get("department") or ""),
        "remote": REMOTE.get(str(row.get("workLocationOption") or "").lower(), ""),
    }
    if places and all(p == "US" or p.replace(" ", "").endswith(",US") for p in places):
        hints["country"] = "US"
    location = "; ".join(str(x) for x in row.get("locations") or [] if x)
    try:
        return Listing(
            company="Microsoft",
            title=str(row.get("name") or ""),
            url=f"{BASE}/careers/job/{pid}",
            location=location[:500],
            posted_at=posted.date() if posted else None,
            ats="eightfold",
            ats_job_id=pid,
            source_ref=str(row.get("displayJobId") or "") or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Newest-first search results for each of opts.queries (US only by default), deduped by posting ID."""
    out: dict[str, Listing] = {}
    first = True
    cut: list[str] = []  # queries cut short by a persistent 429
    error: httpx.HTTPStatusError | None = None
    for q in opts.queries:
        for page in range(MAX_PAGES):
            if len(out) >= opts.max_results:
                return list(out.values())[: opts.max_results]
            if not first:
                time.sleep(PAUSE)
            first = False
            params = {"domain": "microsoft.com", "query": q, "start": page * PAGE, "sort_by": "timestamp"}
            if opts.us_only:
                params["location"] = "United States"
            try:
                reply = _get(SEARCH, params)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                cut.append(q)  # still throttled: keep what we have, try the next query
                error = e
                time.sleep(COOLDOWN)
                break
            rows = ((reply or {}).get("data") or {}).get("positions") or []
            if not isinstance(rows, list) or not rows:
                break
            older = False
            for x in map(_listing, rows):
                if x is None:
                    continue
                if opts.since and x.posted_at and x.posted_at < opts.since:
                    older = True  # newest first: the rest of this query is older too
                    continue
                out.setdefault(x.ats_job_id, x)
            if older or len(rows) < PAGE:
                break
    if cut:
        if not out and error is not None:
            raise error
        log.warning("microsoft_careers: throttled (HTTP 429); results are partial for queries %s", cut)
    return list(out.values())[: opts.max_results]


def describe(listing: Listing) -> str:
    """The posting's full text, from the position_details endpoint (hidden text flagged)."""
    pid = listing.ats_job_id or ""
    if not pid.isdigit():
        raise ValueError("not a Microsoft careers posting id")
    data = (_get(DETAILS, {"position_id": pid, "domain": "microsoft.com", "hl": "en"}) or {}).get("data") or {}
    text, hidden = html_to_text(str(data.get("jobDescription") or ""))
    return mark_hidden(text, hidden)[:100_000]


FEED = register_list(
    ListFeed(
        name="microsoft_careers",
        description="Microsoft Careers (apply.careers.microsoft.com): every Microsoft opening, newest first",
        fetch=fetch,
        describe=describe,
        terms="Public Eightfold search API; robots.txt allows /api/pcsx. Microsoft's terms limit site use to "
        "personal, non-commercial use. Rate-limited (429): ~1 request every 2s, with 429 backoff.",
    )
)
