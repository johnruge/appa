"""JSearch (OpenWeb Ninja) on RapidAPI: Google for Jobs results as structured JSON, so postings from
company career sites, ATSs and job boards all come back from one search.

This is a search API, not one company's board, so a "board" here is one saved search. The slug is the
query with "-" for spaces, e.g. "software-engineer-new-grad" or "junior-developer-in-chicago". board_of()
turns a jsearch.p.rapidapi.com/search?query=... link into such a slug, so seeding one adds the search.

GET https://jsearch.p.rapidapi.com/search?query=...&page=1&num_pages=1&country=us&date_posted=week with
"X-RapidAPI-Key" and "X-RapidAPI-Host: jsearch.p.rapidapi.com" returns {status, data: [{job_id, employer_name,
job_title, job_apply_link, apply_options: [{publisher, apply_link, is_direct}], job_description (plain text),
job_is_remote, job_location, job_country, job_posted_at_datetime_utc, job_employment_types, job_min_salary,
job_max_salary, ...}]} with up to 10 jobs per page. Rows already carry the posting text, so there's no describe().

The key (RAPIDAPI_KEY) needs a RapidAPI account subscribed to JSearch's free Basic plan: 200 requests a
month, hard-limited, 1000/hour. Every page is one request, so a fetch asks for PAGES pages (default 1) and the
module spends at most DAILY_CALLS requests per day per process. That budget is not saved between runs, and
every slug costs at least one request per fetch, so register only 1-2 jsearch slugs and run appa about once a
day. A 429 (quota used up) is logged and ends the fetch with what it has instead of failing. LinkedIn links are
skipped (its terms forbid
automated access); the employer's own link or another publisher's is used instead.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

HOST = "jsearch.p.rapidapi.com"
URL = f"https://{HOST}/search"
KEY_ENV = "RAPIDAPI_KEY"
PAGES = 1  # pages per fetch; each is one request against the 200/month free quota
MAX_PAGES = 50
DAILY_CALLS = 6  # ~200/month; counted per process
PAUSE = 1.0  # seconds between pages
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ID = re.compile(r"^[A-Za-z0-9_=+/-]{1,100}$")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")  # any tag at all counts as HTML
_TYPES = {"FULLTIME": "full_time", "PARTTIME": "part_time", "CONTRACTOR": "contract", "INTERN": "internship"}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no", "on-site": "no", "on_site": "no"}
_LEVELS = {
    "intern": "intern",
    "internship": "intern",
    "entry": "entry",
    "entry_level": "entry",
    "junior": "entry",
    "mid": "mid",
    "senior": "senior",
}
_spent: dict[date, int] = {}


def _check(slug: str) -> str:
    """The search query in a slug like 'software-engineer-new-grad'; ValueError for anything else."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad jsearch slug: {slug!r}")
    return slug.replace("-", " ")


def _key() -> str:
    """The RapidAPI key from the environment; RuntimeError when unset."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    return key


def _budget_left() -> int:
    """Requests this process may still make today."""
    return DAILY_CALLS - _spent.get(date.today(), 0)


def _call(params: dict, key: str) -> dict:
    """One search request, counted against the daily budget (the key travels only in a header)."""
    _spent[date.today()] = _spent.get(date.today(), 0) + 1
    data = get_json(URL, params=params, headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST})
    return data if isinstance(data, dict) else {}


def _date_posted(opts: FeedOptions) -> str:
    """The narrowest date_posted window that still covers opts.since."""
    if not opts.since:
        return "all"
    days = (date.today() - opts.since).days
    return "today" if days <= 1 else "3days" if days <= 3 else "week" if days <= 7 else "month" if days <= 30 else "all"


def _blocked(url: str) -> bool:
    """True for LinkedIn links (never used) and anything that isn't http(s)."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    return p.scheme not in ("http", "https") or host == "linkedin.com" or host.endswith(".linkedin.com")


def _clean(url: str) -> str:
    """The link without Google's utm_* tracking parameters."""
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def _links(r: dict) -> list[str]:
    """Usable apply links, best first: the main apply link, then direct options, then the rest."""
    opts = [o for o in r.get("apply_options") or [] if isinstance(o, dict)]
    ordered = (
        [r.get("job_apply_link")]
        + [o.get("apply_link") for o in opts if o.get("is_direct")]
        + [o.get("apply_link") for o in opts]
    )
    return list(dict.fromkeys(_clean(u) for u in ordered if isinstance(u, str) and u and not _blocked(u)))


def _day(r: dict) -> date | None:
    """The posting date from the ISO datetime or the Unix timestamp."""
    try:
        return date.fromisoformat(str(r.get("job_posted_at_datetime_utc") or "")[:10])
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(int(r.get("job_posted_at_timestamp")), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _salary(r: dict) -> str:
    """'109820-149600 per YEAR', or the source's own salary string, or ''."""
    lo, hi = (str(r.get(k) or "").split(".")[0] for k in ("job_min_salary", "job_max_salary"))
    rng = f"{lo}-{hi}" if lo and hi else f"{lo}+" if lo else f"up to {hi}" if hi else ""
    if rng:
        return f"{rng} per {r.get('job_salary_period') or 'period'}"
    return str(r.get("job_salary_string") or "")


def _hints(r: dict, links: list[str]) -> dict[str, str]:
    """Facts the row states, under HINT_KEYS names."""
    remote = _REMOTE.get(str(r.get("work_arrangement") or "").lower(), "yes" if r.get("job_is_remote") is True else "")
    types = [t for t in r.get("job_employment_types") or [] if isinstance(t, str)]
    edu = r.get("education_required") if isinstance(r.get("education_required"), dict) else {}
    return {
        "remote": remote,
        "country": str(r.get("job_country") or ""),
        "salary": _salary(r),
        "employment_type": ", ".join(_TYPES.get(t, t.lower()) for t in types),
        "level": _LEVELS.get(str(r.get("seniority_level") or "").lower(), ""),
        "degrees": str(edu.get("level") or ""),
        "links": " ".join(links[1:6]),
    }


def _description(r: dict) -> str:
    """The posting text; any HTML (even entity-escaped) is converted with hidden text flagged."""
    raw = str(r.get("job_description") or "")
    if not _HTML.search(raw) and "&lt;" in raw.lower() and _HTML.search(html.unescape(raw)):
        raw = html.unescape(raw)
    if _HTML.search(raw):
        text, hidden = html_to_text(raw)
        raw = mark_hidden(text, hidden)
    return raw[:MAX_DESC]


def _listing(r: dict) -> Listing:
    """One search row as a Listing (ValueError when malformed or only on LinkedIn)."""
    if not isinstance(r, dict):
        raise ValueError("row is not an object")
    links = _links(r)
    if not links:
        raise ValueError("no usable apply link")
    url, jid = links[0], str(r.get("job_id") or "").strip()
    ref, key = parse_url(url), job_key(url)
    ats_id = key.split(":", 1)[1] if key else jid if _ID.fullmatch(jid) else None
    location = str(r.get("job_location") or "") or ", ".join(
        str(r.get(k)) for k in ("job_city", "job_state", "job_country") if r.get(k)
    )
    return Listing(
        company=str(r.get("employer_name") or ""),
        title=str(r.get("job_title") or ""),
        url=url,
        location=location[:500],
        posted_at=_day(r),
        description=_description(r),
        ats=ref.ats if ref else "unknown",
        ats_job_id=ats_id[:100] if ats_id else None,
        source_ref=jid[:300] or None,
        hints=_hints(r, links),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Postings the saved search finds (PAGES pages of 10, within the daily request budget)."""
    query, opts = _check(slug), opts or FeedOptions()
    key = _key()
    params = {
        "query": query,
        "num_pages": "1",
        "date_posted": _date_posted(opts),
        **({"country": "us"} if opts.us_only else {}),
    }
    out: dict[str, Listing] = {}
    for page in range(1, min(PAGES, MAX_PAGES) + 1):
        if _budget_left() <= 0:
            log.warning("jsearch daily request budget (%d) used up; stopping", DAILY_CALLS)
            break
        if page > 1:
            time.sleep(PAUSE)
        try:
            rows = _call({**params, "page": str(page)}, key).get("data")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                raise
            log.warning("jsearch quota or rate limit hit (429); stopping with %d postings", len(out))
            break
        rows = rows.get("jobs") if isinstance(rows, dict) else rows  # the newer API nests jobs under data
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            try:
                x = _listing(r)
            except ValueError as e:
                log.debug("skipping jsearch row: %s", e)
                continue
            if opts.since and x.posted_at and x.posted_at < opts.since:
                continue
            out.setdefault(x.ats_job_id or x.url, x)
        if len(out) >= opts.max_results:
            break
    return list(out.values())[: opts.max_results]


def board_of(url: str) -> str | None:
    """The saved-search slug of a jsearch.p.rapidapi.com/search?query=... link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host != HOST or p.path.rstrip("/") != "/search":
        return None
    vals = parse_qs(p.query).get("query") or []
    if len(vals) != 1:
        return None
    slug = re.sub(r"\s+", "-", vals[0].strip())
    try:
        _check(slug)
    except ValueError:
        return None
    return slug


BOARD = register_board(
    BoardFeed(
        ats="jsearch_rapidapi",
        fetch=fetch,
        board_of=board_of,
        example="software-engineer-new-grad",
        terms=f"JSearch by OpenWeb Ninja via RapidAPI; needs {KEY_ENV} (free RapidAPI account + JSearch Basic plan: "
        "200 requests/month hard limit, 1000/hour). Each page is one request; this source makes at most "
        f"{DAILY_CALLS}/day per process (not saved between runs: register only 1-2 jsearch slugs). "
        "Terms allow use of API data in your own product, not resale as a "
        "standalone data/API product. LinkedIn links are skipped.",
        hosts=(HOST,),
    )
)
