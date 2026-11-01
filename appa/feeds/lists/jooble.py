"""Jooble REST API: a job search engine that aggregates postings from thousands of US sites and boards
(https://jooble.org/api/about, docs at help.jooble.org "REST API Documentation"). Needs a free API key
(JOOBLE_API_KEY) requested through Jooble's form; a jooble.org key covers US listings only. appa sends
one POST per query and page to https://jooble.org/api/{key} and reads the jobs array (title, company,
location, a short snippet, salary, type, link to the posting on jooble.org, updated timestamp, id).
Results carry only a short snippet, which becomes the description (prefixed by the title/company/location
line, so it is never empty): appa must never fall back to fetching the jooble.org link, because robots.txt
disallows /desc/ and /away/ and Cloudflare blocks bots there. The free key has a lifetime cap of 500
requests (not monthly), so each query reads at most a few pages.
UNVERIFIED: no key was available, so the fixture follows the documented example rather than a recorded
response, and it is unknown whether Cloudflare lets appa's client through. Jooble offers the API to
publishers who show its results on their site; personal, non-publishing use is unconfirmed. Off by default.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from urllib.parse import quote

import httpx

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import post_json
from appa.sources.text import html_to_text, mark_hidden

BASE = "https://jooble.org/api/"
PAGE_SIZE = 100  # ResultOnPage asked for each request
MAX_PAGES = 5  # per query: the key's 500 lifetime requests must last
PAUSE = 0.5  # seconds between requests
_TYPES = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "temporary": "temporary",
    "internship": "internship",
}


def _key() -> str:
    """The API key from the environment; raises RuntimeError when it is missing."""
    key = os.environ.get("JOOBLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JOOBLE_API_KEY is not set")
    return key


def _post(key: str, body: dict) -> dict:
    """One search request; errors are re-raised without the URL, which contains the key."""
    try:
        data = post_json(BASE + quote(key, safe=""), body, headers={"Content-Type": "application/json"})
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Jooble API returned HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"Jooble API request failed ({type(e).__name__})") from None
    except ValueError:
        raise RuntimeError("Jooble API returned a non-JSON response") from None
    return data if isinstance(data, dict) else {}


def _date(s) -> date | None:
    """An ISO timestamp from the API (e.g. '2023-09-15T12:55:35.3870000') as a date, or None."""
    try:
        return datetime.fromisoformat(str(s)[:19]).date()
    except (TypeError, ValueError):
        return None


def _description(job: dict) -> str:
    """The snippet as text, after a header line built from the row, so it is never empty."""
    head = " | ".join(
        str(job.get(k) or "").strip()
        for k in ("title", "company", "location", "salary", "type")
        if str(job.get(k) or "").strip()
    )
    text, hidden = html_to_text(str(job.get("snippet") or ""))
    body = mark_hidden(" ".join(text.split()), hidden).strip()  # snippets are one line of inline HTML
    return f"{head}\n\n{body or '(Jooble gives only this summary; open the link by hand for the full text.)'}"[:100_000]


def _listing(job: dict) -> Listing | None:
    """A Listing from one result, or None for malformed rows."""
    location, kind = str(job.get("location") or ""), str(job.get("type") or "").strip()
    hints = {"salary": str(job.get("salary") or ""), "employment_type": _TYPES.get(kind.lower(), kind)}
    if "remote" in location.lower():
        hints["remote"] = "yes"
    job_id = str(job.get("id") or "").strip()[:100] or None
    try:
        return Listing(
            company=str(job.get("company") or ""),
            title=str(job.get("title") or ""),
            url=str(job.get("link") or ""),
            location=location[:500],
            posted_at=_date(job.get("updated")),
            description=_description(job),
            ats="jooble",
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def _search(key: str, query: str, limit: int) -> list[Listing]:
    """One keyword search in the US, page by page, until results run out, MAX_PAGES, or `limit` rows."""
    out: list[Listing] = []
    seen_rows = 0
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        data = _post(key, {"keywords": query, "location": "USA", "page": page, "ResultOnPage": PAGE_SIZE})
        jobs = [j for j in data.get("jobs") or [] if isinstance(j, dict)]
        if not jobs:
            break
        seen_rows += len(jobs)
        out += [x for x in map(_listing, jobs) if x is not None]
        try:
            total = int(data.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        if seen_rows >= total or len(out) >= limit:
            break
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """US postings matching each of opts.queries (newest first, deduped by Jooble id), since opts.since."""
    key = _key()
    seen: set[str] = set()
    out: list[Listing] = []
    for i, query in enumerate(opts.queries):
        if len(out) >= opts.max_results:
            break
        if i:
            time.sleep(PAUSE)
        for x in _search(key, query, opts.max_results - len(out)):
            k = x.ats_job_id or x.url
            if k not in seen and not (opts.since and x.posted_at and x.posted_at < opts.since):
                seen.add(k)
                out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="jooble",
        description="Jooble (job search aggregator): US postings from many sites via the REST API; snippets only "
        "(unverified, off by default)",
        fetch=fetch,
        key_env="JOOBLE_API_KEY",
        terms="Free API key via the form at jooble.org/api/about, offered to publishers who show Jooble results on "
        "their site: personal, non-publishing use is UNCONFIRMED (ask Jooble before enabling). Adapter is "
        "unverified against a live response. The free key has a lifetime limit of 500 requests; a jooble.org "
        "key covers US jobs only. Links open on jooble.org (robots.txt disallows bots on /desc/ and /away/): "
        "the snippet is the description so appa never fetches them; open them by hand.",
        default=False,
    )
)
