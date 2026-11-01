"""jobdataapi.com, an aggregator of postings pulled from 80+ ATSs and company career sites.

GET https://jobdataapi.com/api/jobs/ returns {count, next, previous, results}, newest first, each result with
the company (name, website, is_agency), title, location, types, countries, published time, the HTML
description, experience_level (EN/MI/SE/EX), salary and application_url (the employer's own ATS/careers
link, which is what appa applies on).

Without a key the API is public but throttled: ~10 requests per hour, 20 results per request, no further
pages and no date slicing. With JOBDATAAPI_KEY (a paid plan, sent as "Authorization: Api-Key ...") pages of
up to 4000 rows, published_since and ~10 requests/second are allowed. Two entry points share it:

- the list feed "jobdataapi": one title search per FeedOptions query (US only by default);
- the board "jobdataapi": one company's postings by its website domain (slug "walgreens.com").
  Postings never link to jobdataapi.com, so boards are only found from jobdataapi API links.

Terms (jobdataapi.com/accounts/pricing, "Our terms"): data may not be used to build a competing API. robots.txt
disallows /api/ for crawlers; this is the documented API, not crawling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from appa.core.ats_urls import parse_url
from appa.feeds import BoardFeed, FeedOptions, ListFeed, Listing, register_board, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

URL = "https://jobdataapi.com/api/jobs/"
KEY_ENV = "JOBDATAAPI_KEY"
ANON_QUERIES = 8  # anonymous calls are ~10/hour; leave a little room for the board and retries
MAX_PAGES = 50
MAX_DESC = 100_000
_DOMAIN = re.compile(r"^(?=.{4,100}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LEVELS = {"EN": "entry"}  # MI/SE/EX are left out: the source labels e.g. "Software Engineer I" as SE
_TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "internship": "internship",
    "contract": "contract",
    "temporary": "temporary",
    "freelance": "contract",
}


def _check(slug: str) -> str:
    """The slug, if it is a plain lowercase company domain like 'walgreens.com'; else ValueError."""
    if not isinstance(slug, str) or not _DOMAIN.fullmatch(slug.lower()):
        raise ValueError(f"bad jobdataapi slug: {slug!r}")
    return slug.lower()


def _host(url) -> str:
    """Lowercase hostname of a link without 'www.', or ''."""
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _day(v) -> date | None:
    """'2026-10-30T21:55:43Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _get(params: dict, key: str | None) -> dict:
    """One API call, retrying 5xx twice with backoff; other HTTP errors raise (the key is only in a header)."""
    headers = {"Authorization": f"Api-Key {key}"} if key else None
    for attempt in range(3):
        try:
            data = get_json(URL, params=params, headers=headers)
            return data if isinstance(data, dict) else {}
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500 or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def _salary(r: dict) -> str:
    """'73800-106000 USD/year', '150000+ USD/year', or '' when the posting has no pay."""
    lo, hi = (str(r.get(k) or "").split(".")[0] for k in ("salary_min", "salary_max"))
    rng = f"{lo}-{hi}" if lo and hi else f"{lo}+" if lo else f"up to {hi}" if hi else ""
    return f"{rng} {r.get('salary_currency') or ''}".strip() + "/year" if rng else ""


def _hints(r: dict) -> dict[str, str]:
    """The facts the posting states, under HINT_KEYS names."""
    types = [str(t.get("name") or "") for t in r.get("types") or [] if isinstance(t, dict)]
    level = (
        "intern" if any(t.lower() == "internship" for t in types) else _LEVELS.get(str(r.get("experience_level")), "")
    )
    codes = [str(c.get("code")) for c in r.get("countries") or [] if isinstance(c, dict) and c.get("code")]
    return {
        "level": level,
        "employment_type": ", ".join(_TYPES.get(t.lower(), t) for t in types if t),
        "country": ", ".join(dict.fromkeys(codes)),
        "salary": _salary(r),
    }


def _description(r: dict) -> str:
    """The HTML description as text, hidden text flagged."""
    text, hidden = html_to_text(str(r.get("description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(r: dict) -> Listing:
    """One result as a Listing (ValueError when it is malformed, an agency post, or has no apply link)."""
    if not isinstance(r, dict) or not isinstance(r.get("company"), dict):
        raise ValueError("not a job result")
    if r["company"].get("is_agency"):
        raise ValueError("agency posting")
    url = str(r.get("application_url") or "")
    ref = parse_url(url)
    jid = str(r.get("id") or "") or None
    ext = str(r.get("ext_id") or "").strip()
    location = "; ".join(dict.fromkeys(p.strip() for p in str(r.get("location") or "").split(";") if p.strip()))
    return Listing(
        company=str(r["company"].get("name") or ""),
        title=str(r.get("title") or ""),
        url=url,
        location=location[:500],
        posted_at=_day(r.get("published")),
        description=_description(r),
        ats=ref.ats if ref else "unknown",
        ats_job_id=(ext if 0 < len(ext) <= 100 else jid),
        source_ref=jid,
        hints=_hints(r),
    )


def _collect(params: dict, opts: FeedOptions, key: str | None, out: dict[str, Listing], keep=None) -> None:
    """Run one search into `out` (keyed by ats_job_id/url): one page without a key, paged with one."""
    params = dict(params)
    if key:
        params["page_size"] = max(1, min(1000, opts.max_results))
        if opts.since:
            params["published_since"] = opts.since.isoformat()
    for page in range(1, (MAX_PAGES if key else 1) + 1):
        if key and page > 1:
            params["page"] = page
            time.sleep(0.3)
        data = _get(params, key)
        rows = data.get("results") or []
        for r in rows:
            try:
                x = _listing(r)
            except ValueError as e:
                log.debug("skipping jobdataapi row: %s", e)
                continue
            if (opts.since and x.posted_at and x.posted_at < opts.since) or (keep and not keep(r)):
                continue
            out.setdefault(x.ats_job_id or x.url, x)
            if len(out) >= opts.max_results:
                return
        if not rows or not data.get("next"):
            return


def fetch_list(opts: FeedOptions) -> list[Listing]:
    """Newest postings for each title query (US only by default), deduped across queries."""
    key = os.environ.get(KEY_ENV) or None
    queries = [q for q in dict.fromkeys(q.strip() for q in opts.queries) if len(q) >= 3]
    out: dict[str, Listing] = {}
    for i, q in enumerate(queries if key else queries[:ANON_QUERIES]):
        if i:
            time.sleep(0.3 if key else 1.0)
        params = {"title": q, **({"country_code": "US"} if opts.us_only else {})}
        _collect(params, opts, key, out)
        if len(out) >= opts.max_results:
            break
    return list(out.values())[: opts.max_results]


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Newest postings of the company whose website is this domain (20 without a key, all with one)."""
    slug, opts = _check(slug), opts or FeedOptions()
    key = os.environ.get(KEY_ENV) or None
    params = {"company_website": slug, **({"country_code": "US"} if opts.us_only else {})}
    out: dict[str, Listing] = {}

    def same_company(r: dict) -> bool:
        """The filter is a substring match; keep only the company whose site is exactly this domain."""
        h = _host(r["company"].get("website_url"))
        return h == slug or h.endswith("." + slug)

    _collect(params, opts, key, out, keep=same_company)
    return list(out.values())[: opts.max_results]


def board_of(url: str) -> str | None:
    """The company domain in a jobdataapi.com jobs API link (?company_website=...), or None."""
    if _host(url) != "jobdataapi.com":
        return None
    p = urlparse(url.strip())
    vals = parse_qs(p.query).get("company_website") or []
    if not p.path.startswith("/api/jobs") or len(vals) != 1:
        return None
    try:
        return _check(_host(vals[0]) if "://" in vals[0] else vals[0].removeprefix("www."))
    except ValueError:
        return None


_TERMS = (
    "Public API, no key needed but ~10 requests/hour, 20 newest results per search, no paging; set "
    f"{KEY_ENV} (paid plan) for paging and published_since. Terms forbid building a competing API."
)

FEED = register_list(
    ListFeed(
        name="jobdataapi",
        fetch=fetch_list,
        key_env=KEY_ENV,  # the keyless tier answers 403 since 2026-10-31
        terms=_TERMS,
        description="jobdataapi.com: ATS-sourced postings from 80,000+ companies, newest first, title search.",
    )
)

BOARD = register_board(
    BoardFeed(
        ats="jobdataapi",
        fetch=fetch,
        board_of=board_of,
        example="walgreens.com",
        terms=_TERMS,
        hosts=("jobdataapi.com",),
    )
)
