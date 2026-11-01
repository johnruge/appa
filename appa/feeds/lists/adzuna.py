"""Adzuna: a job search engine that aggregates millions of postings from employer sites and boards, with
an official search API (https://developer.adzuna.com). It needs a free account: set ADZUNA_APP_ID and
ADZUNA_APP_KEY. appa runs one date-sorted US search per query (50 results a page) and keeps the free
tier's limit in mind (about 250 calls a day, 25 a minute): at most a few pages per query, a pause between
calls, and a hard cap on calls per run. Results carry only a snippet of the description (a few hundred
characters), which appa keeps as the description: the landing page (www.adzuna.com/land/ad/<id>) sits
behind CloudFront and answers automated clients with HTTP 403, so the full text is only available by
clicking through by hand. Ads with no employer name are kept as "Unknown (via Adzuna)".

The app_key travels in the query string. This module's errors never include the URL, but httpx's own
INFO logger prints request URLs, so don't enable INFO logging for "httpx" while this feed runs.

Not yet verified against live data (no key was available when it was written): the fixture follows the
documented response shape (developer.adzuna.com/docs/search), not a recorded response.
"""

from __future__ import annotations

import math
import os
import time
from datetime import date, datetime
from urllib.parse import quote

import httpx

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
PER_PAGE = 50  # the API's maximum
MAX_PAGES_PER_QUERY = 4  # free tier: ~250 calls/day, so a run stays small
MAX_CALLS = 20  # hard cap on API calls per fetch
PAUSE = 2.5  # seconds between calls (free tier allows ~25 a minute)
NO_COMPANY = "Unknown (via Adzuna)"
MAX_DESC = 100_000


def _date(value) -> date | None:
    """The API's ISO 'created' timestamp as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _salary(row: dict) -> str:
    """The posted pay range, or "" when Adzuna only predicted it (or gives none)."""
    if str(row.get("salary_is_predicted") or "0") != "0":
        return ""
    lo, hi = row.get("salary_min"), row.get("salary_max")
    try:
        vals = [f"${float(v):,.0f}" for v in (lo, hi) if v not in (None, "")]
    except (TypeError, ValueError):
        return ""
    return "-".join(dict.fromkeys(vals))


def _snippet(raw) -> str:
    """The result's description snippet as plain text, with hidden text flagged."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(" ".join(text.split()), hidden)[:MAX_DESC]


def _listing(row: dict) -> Listing | None:
    """A Listing from one search result, or None for malformed rows."""
    if not isinstance(row, dict):
        return None
    company = row.get("company").get("display_name") if isinstance(row.get("company"), dict) else ""
    company = str(company or "").strip() or NO_COMPANY
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    category = row.get("category") if isinstance(row.get("category"), dict) else {}
    url, title, job_id = str(row.get("redirect_url") or ""), str(row.get("title") or ""), str(row.get("id") or "")
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if not (url.startswith("https://") and title.strip() and job_id):
        return None
    hints = {
        "country": "US",
        "category": str(category.get("label") or ""),
        "salary": _salary(row),
        "employment_type": str(row.get("contract_time") or row.get("contract_type") or ""),
    }
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=str(location.get("display_name") or ""),
            posted_at=_date(row.get("created")),
            description=_snippet(row.get("description")),
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def _page(country: str, page: int, params: dict) -> dict:
    """One search page; errors are re-raised without the request URL, which carries the API key."""
    try:
        data = get_json(
            URL.format(country=quote(country, safe=""), page=page),
            params=params,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Adzuna search failed: HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"Adzuna search failed: {type(e).__name__}") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Adzuna response has no results list")
    return data


def fetch(opts: FeedOptions) -> list[Listing]:
    """Newest US postings for each of opts.queries (date-sorted, a few pages each), deduped by Adzuna ID."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not app_key:
        raise RuntimeError("ADZUNA_APP_KEY is not set")
    if not app_id:
        raise RuntimeError("ADZUNA_APP_ID is not set")
    base = {"app_id": app_id, "app_key": app_key, "results_per_page": PER_PAGE, "sort_by": "date"}
    if opts.since:
        base["max_days_old"] = max(1, (date.today() - opts.since).days + 1)
    pages = max(1, min(MAX_PAGES_PER_QUERY, math.ceil(opts.max_results / PER_PAGE)))
    out, seen, calls = [], set(), 0
    for query in opts.queries:
        for page in range(1, pages + 1):
            if calls >= MAX_CALLS or len(out) >= opts.max_results:
                return out[: opts.max_results]
            if calls:
                time.sleep(PAUSE)
            calls += 1
            rows = _page("us", page, {**base, "what": query})["results"]
            for x in map(_listing, rows):
                if x is not None and x.ats_job_id not in seen:
                    seen.add(x.ats_job_id)
                    out.append(x)
            if len(rows) < PER_PAGE:
                break
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="adzuna",
        description="Adzuna search API: aggregated US postings from many boards and employer sites, newest first",
        fetch=fetch,
        key_env="ADZUNA_APP_KEY",
        extra_env=("ADZUNA_APP_ID",),
        terms="Free API key (register at developer.adzuna.com); personal job search is a permitted use. Free tier "
        "is ~250 calls/day and 25/minute. Credit Adzuna and link to its listing when displaying ads; one "
        "account per person; only snippets of descriptions are returned, and the adzuna.com landing pages "
        "block automated clients (HTTP 403), so full text needs a manual click-through.",
    )
)
