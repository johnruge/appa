"""Google for Jobs, read through SerpApi's Google Jobs engine (serpapi.com/google-jobs-api).

Google for Jobs aggregates postings from nearly every ATS, company site and job board. This is a paid
search API, so it's a list source that is off by default (default=False): appa runs one search per
opts.queries entry (or a few built-in new-grad queries), across the United States when opts.us_only, and
spends at most MAX_CALLS searches per run. Nothing here is discovered from links, so untrusted postings
can't make appa spend the quota.

GET https://serpapi.com/search.json?engine=google_jobs&q=...&location=...&hl=en&gl=us&api_key=... returns
{jobs_results: [{title, company_name, location, via, description, detected_extensions: {posted_at,
schedule_type, salary, work_from_home}, apply_options: [{title, link}], job_id}], serpapi_pagination:
{next_page_token}}, up to 10 results per page. A result's link is its best apply option: a known ATS first,
then a company site, then a job board; LinkedIn options are ignored and LinkedIn-only results are dropped.
opts.since becomes Google's own date filter ("since yesterday", "in the last week"...).

Needs SERPAPI_API_KEY (free plan: 250 searches/month, 50/hour; every page is one search, cached repeats
within an hour are free). The key travels in the query string: this module's errors never include the URL,
but httpx's own INFO logger prints request URLs, so don't enable INFO logging for "httpx" while it runs.

Not yet verified against live data (no key was available when it was written): the fixture follows the
documented response shape (serpapi.com/google-jobs-api), not a recorded response. Unverified details: the
base64 job_id carrying Google's htidocid, apply link utm parameters, and whether description is HTML.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import urlparse, urlunparse

import httpx

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://serpapi.com/search.json"
KEY_ENV = "SERPAPI_API_KEY"
PAGE_SIZE = 10  # Google returns up to 10 results per page
MAX_PAGES = 2  # pages per query: every page spends one search of a small monthly quota
MAX_CALLS = 6  # searches per fetch, across all queries
PAUSE = 0.5  # seconds between searches
MAX_DESC = 100_000
QUERIES = ("new grad software engineer", "entry level software engineer")
_AGO = re.compile(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s+ago", re.I)
_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30}
_NO_RESULTS = "hasn't returned any results"
_TYPES = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contractor": "contract",
    "internship": "internship",
    "temporary": "temporary",
    "per diem": "per_diem",
}
# Job boards and aggregators: used only when a result has no ATS or company apply link.
_BOARDS = (
    "indeed.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "dice.com",
    "monster.com",
    "talent.com",
    "bebee.com",
    "simplyhired.com",
    "careerbuilder.com",
    "jooble.org",
    "adzuna.com",
    "lensa.com",
    "builtin.com",
    "wellfound.com",
    "salary.com",
    "snagajob.com",
    "jobright.ai",
    "joinhandshake.com",
    "learn4good.com",
    "teal.com",
    "jobleads.com",
    "whatjobs.com",
    "jobilize.com",
    "recruit.net",
    "clearancejobs.com",
)


def _host(url: str) -> str:
    """Lowercase hostname of a link, or ''."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return ""


def _on(host: str, domains) -> bool:
    """True when host is one of these domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _key() -> str:
    """The API key from the environment; RuntimeError when unset."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    return key


def _call(params: dict) -> dict:
    """One search; errors are re-raised without the URL (it carries the key). No results -> {}."""
    try:
        data = get_json(API, params=params)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"serpapi request failed: HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"serpapi request failed: {type(e).__name__}") from None
    data = data if isinstance(data, dict) else {}
    err = str(data.get("error") or "")
    if err and _NO_RESULTS not in err:
        raise RuntimeError(f"serpapi error: {err[:200]}")
    return {} if err else data


def _date_filter(since: date | None) -> str:
    """Google's own date-posted phrase for opts.since, or '' (older than a month: no filter)."""
    if not since:
        return ""
    days = (date.today() - since).days
    for limit, phrase in (
        (1, "since yesterday"),
        (3, "in the last 3 days"),
        (7, "in the last week"),
        (31, "in the last month"),
    ):
        if days <= limit:
            return phrase
    return ""


def _posted(text) -> date | None:
    """'22 hours ago' / '3 days ago' / '1 month ago' -> an approximate date, or None."""
    m = _AGO.search(str(text or ""))
    if not m:
        return None
    return date.today() - timedelta(days=int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()])


def _clean(url: str) -> str:
    """The link without Google's utm_* tracking parameters; any other link is returned unchanged."""
    url = url.strip()
    p = urlparse(url)
    parts = p.query.split("&") if p.query else []
    keep = [x for x in parts if not x.lower().startswith("utm_")]
    return url if len(keep) == len(parts) else urlunparse(p._replace(query="&".join(keep)))


def _rank(url: str) -> int:
    """Lower is better: a known ATS link, then a company site with a job ID, a company site, a board link."""
    host = _host(url)
    if parse_url(url):
        return 0
    if not _on(host, _BOARDS):
        return 1 if job_key(url) else 2
    return 3 if job_key(url) else 4


def _apply_links(r: dict) -> list[str]:
    """The result's http(s) apply links, LinkedIn excluded, best first."""
    links = []
    for o in r.get("apply_options") or []:
        link = str(o.get("link") or "") if isinstance(o, dict) else ""
        host = _host(link)
        if link.lower().startswith(("http://", "https://")) and host and not _on(host, ("linkedin.com",)):
            links.append(_clean(link))
    return sorted(dict.fromkeys(links), key=_rank)


def _google_id(job_id) -> str | None:
    """Google's posting id (htidocid) inside the base64 job_id, or the job_id itself when short enough."""
    s = str(job_id or "").strip()
    try:
        doc = json.loads(base64.b64decode(s + "=" * (-len(s) % 4), validate=False)).get("htidocid")
    except (ValueError, AttributeError, TypeError):
        doc = None
    if isinstance(doc, str) and 0 < len(doc) <= 100:
        return doc
    return s if 0 < len(s) <= 100 else None


def _hints(r: dict, others: list[str]) -> dict[str, str]:
    """The facts the result states, under HINT_KEYS names."""
    ext = r.get("detected_extensions") if isinstance(r.get("detected_extensions"), dict) else {}
    sched = str(ext.get("schedule_type") or "")
    return {
        "employment_type": ", ".join(_TYPES.get(t.strip().lower(), t.strip()) for t in sched.split(" and ") if t),
        "salary": str(ext.get("salary") or ""),
        "remote": "yes" if ext.get("work_from_home") is True else "",
        "links": " ".join(others),
    }


def _listing(r: dict) -> Listing:
    """One result as a Listing (ValueError when malformed or its only apply option is LinkedIn)."""
    if not isinstance(r, dict):
        raise ValueError("result is not an object")
    links = _apply_links(r)
    if not links:
        raise ValueError("no non-LinkedIn apply link")
    url, ref = links[0], parse_url(links[0])
    key = job_key(url)
    site_id = key.split(":", 1)[1] if key else ""
    ext = r.get("detected_extensions") if isinstance(r.get("detected_extensions"), dict) else {}
    text, hidden = html_to_text(str(r.get("description") or ""))
    return Listing(
        company=str(r.get("company_name") or ""),
        title=str(r.get("title") or ""),
        url=url,
        location=str(r.get("location") or "")[:500],
        posted_at=_posted(ext.get("posted_at")),
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats=ref.ats if ref else "unknown",
        ats_job_id=site_id if 0 < len(site_id) <= 100 else None,
        source_ref=_google_id(r.get("job_id")),
        hints=_hints(r, links[1:]),
    )


def fetch(opts: FeedOptions) -> list[Listing]:
    """One search per query (a few pages each, at most MAX_CALLS searches), deduped by Listing.key."""
    key = _key()
    queries = [" ".join(q.split())[:200] for q in (opts.queries or QUERIES)]
    queries = list(dict.fromkeys(q for q in queries if q)) or list(QUERIES)
    pages = max(1, min(MAX_PAGES, -(-opts.max_results // PAGE_SIZE)))
    when = _date_filter(opts.since)
    out: dict[str, Listing] = {}
    calls = 0
    for query in queries:
        params = {"engine": "google_jobs", "q": f"{query} {when}".strip(), "hl": "en", "api_key": key}
        if opts.us_only:
            params.update(location="United States", gl="us")
        for _ in range(pages):
            if calls >= MAX_CALLS or len(out) >= opts.max_results:
                return list(out.values())[: opts.max_results]
            if calls:
                time.sleep(PAUSE)
            calls += 1
            data = _call(params)
            rows = data.get("jobs_results") if isinstance(data.get("jobs_results"), list) else []
            for r in rows:
                try:
                    x = _listing(r)
                except ValueError as e:
                    log.debug("skipping google jobs result: %s", e)
                    continue
                if opts.since and x.posted_at and x.posted_at < opts.since:
                    continue
                out.setdefault(x.key, x)
            pag = data.get("serpapi_pagination") if isinstance(data.get("serpapi_pagination"), dict) else {}
            token = str(pag.get("next_page_token") or "")
            if not rows or not token:
                break
            params = {**params, "next_page_token": token}
    return list(out.values())[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="serpapi_google_jobs",
        description="Google for Jobs via SerpApi: postings from nearly every ATS, company site and board, one search "
        "per query (paid API, off by default)",
        fetch=fetch,
        key_env=KEY_ENV,
        terms=f"SerpApi Google Jobs API, needs {KEY_ENV} (free plan: 250 searches/month, 50/hour; each page is one "
        f"search, at most {MAX_CALLS} per run). SerpApi terms forbid reselling or redistributing the service; "
        "personal use only.",
        default=False,
    )
)
