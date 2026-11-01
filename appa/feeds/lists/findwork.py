"""Findwork.dev: a search engine for tech jobs that aggregates postings from HN Who's Hiring, YC
startups, Remote OK, WeWorkRemotely, Python.org and other boards. Its REST API
(GET https://findwork.dev/api/jobs/) needs a free API key (FINDWORK_API_KEY, from an account at
findwork.dev/developers), sent as "Authorization: Token <key>". Results are paginated (count, next,
results); each row carries the posting HTML, so no describe() is needed. appa runs one search per
query and walks pages with ?page=N itself (never following the API's "next" link, so the key is only
ever sent to findwork.dev). Searches ask for sort_by=date and stop once a whole page (in date order)
is older than opts.since.

Not yet verified against a live keyed response: the field names come from the public posting pages
and the old API docs. So the feed is off by default (default=False), and a non-empty page that yields
no listings raises instead of quietly returning nothing.
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import date, datetime
from urllib.parse import urlsplit

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://findwork.dev/api/jobs/"
MAX_PAGES = 50
PAUSE = 1.0  # seconds between requests; the API is rate limited per key
_TYPES = {
    "full time": "full_time",
    "full-time": "full_time",
    "part time": "part_time",
    "part-time": "part_time",
    "contract": "contract",
    "internship": "internship",
}
_LINK = re.compile(r'href="(https?://[^"\s]{8,500})"')


def _headers() -> dict:
    """The auth header; raises RuntimeError when the key is missing."""
    key = os.environ.get("FINDWORK_API_KEY")
    if not key:
        raise RuntimeError("FINDWORK_API_KEY is not set")
    return {"Authorization": f"Token {key}"}


def _date(s) -> date | None:
    """An ISO timestamp from the API as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(s)[:19]).date()
    except (TypeError, ValueError):
        return None


def _links(raw_html: str) -> str:
    """Whole non-Findwork http(s) links in the description, space separated, at most 300 chars."""
    out: list[str] = []
    for h in _LINK.findall(raw_html):
        h = html.unescape(h).strip()
        try:
            host = (urlsplit(h).hostname or "").lower()
        except ValueError:
            continue
        if not host or host == "findwork.dev" or host.endswith(".findwork.dev") or h in out:
            continue
        if len(" ".join(out + [h])) > 300:
            break
        out.append(h)
    return " ".join(out)


def _older(found: list[Listing], since: date | None) -> bool:
    """True when the page is in newest-first order and every posting is older than since."""
    days = [x.posted_at for x in found]
    if not since or not days or None in days:
        return False
    return all(a >= b for a, b in zip(days, days[1:], strict=False)) and days[0] < since


def _listing(row: dict) -> Listing | None:
    """A Listing from one result row, or None for malformed rows."""
    raw = str(row.get("text") or "")
    text, hidden = html_to_text(raw) if raw else ("", [])
    hints = {
        "employment_type": _TYPES.get(str(row.get("employment_type") or "").lower(), ""),
        "remote": "yes" if row.get("remote") is True else "",
        "links": _links(raw),
    }
    job_id = str(row.get("id") or "")[:100] or None
    try:
        return Listing(
            company=str(row.get("company_name") or ""),
            title=str(row.get("role") or ""),
            url=str(row.get("url") or ""),
            location=str(row.get("location") or "")[:500],
            posted_at=_date(row.get("date_posted")),
            description=mark_hidden(text, hidden)[:100_000],
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def _search(query: str, opts: FeedOptions, headers: dict, limit: int) -> list[Listing]:
    """One keyword search, page by page, until there's no next page or `limit` rows are read."""
    out: list[Listing] = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        data = get_json(URL, params={"search": query, "sort_by": "date", "page": page}, headers=headers) or {}
        rows = data.get("results") or []
        if not rows:
            break
        found = [x for x in (_listing(r) for r in rows if isinstance(r, dict)) if x is not None]
        if not found:
            raise RuntimeError(f"findwork: none of {len(rows)} rows parsed; the API response shape may have changed")
        if _older(found, opts.since):
            break
        out += [x for x in found if not opts.since or x.posted_at is None or x.posted_at >= opts.since]
        if not data.get("next") or len(out) >= limit:
            break
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """Tech jobs matching each of opts.queries (newest first, deduped by Findwork job id)."""
    headers = _headers()
    seen: set[str] = set()
    out: list[Listing] = []
    for i, query in enumerate(opts.queries):
        if len(out) >= opts.max_results:
            break
        if i:
            time.sleep(PAUSE)
        for x in _search(query, opts, headers, opts.max_results - len(out)):
            k = x.ats_job_id or x.url
            if k not in seen:
                seen.add(k)
                out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="findwork",
        description="Findwork.dev: tech jobs aggregated from HN, YC, Remote OK, WeWorkRemotely and more (API key)",
        fetch=fetch,
        key_env="FINDWORK_API_KEY",
        default=False,
        terms="Free API key from a findwork.dev account (findwork.dev/developers). Postings are aggregated from other "
        "boards; the url is Findwork's page, which links to the original. Keep requests modest (rate "
        "limited; unauthenticated calls show a limit of 10). Off by default until checked against a real keyed "
        "response.",
    )
)
