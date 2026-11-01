"""Eightfold AI career sites ({company}.eightfold.ai/careers, e.g. Netflix, Qualcomm), through the public JSON
search the site's own page uses. No login is needed to view, and robots.txt allows /api/apply and /api/pcsx.

A board slug is "company:domain" (e.g. netflix:netflix.com): the eightfold.ai subdomain plus the `domain`
query parameter every Eightfold page and API call carries. Tenants run one of two APIs: the older
/api/apply/v2/jobs, or the newer /api/pcsx/search (the old one answers 403 there), so fetch tries the old
one and switches. Pages hold 10 rows; appa runs one search per opts.queries, newest first, filtered to the
United States with opts.us_only, and caps pages. Search rows carry no description: describe() reads one
posting's text from the detail endpoint. Listing links point at {company}.eightfold.ai/careers/job/{id},
which redirects to the company's own careers domain; the row's canonicalPositionUrl (that custom-domain link)
goes in hints["links"] so the two copies can be matched. The country hint is "US" for every US spelling.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

PAGE = 10  # Eightfold's fixed page size
MAX_PAGES = 50  # per query
PAUSE = 1.0  # seconds between requests (Eightfold answers 429 to fast clients)
PCSX_PAUSE = 2.0  # the newer API rate-limits harder
BACKOFF = 15.0  # one wait-and-retry after a 429
MAX_DESC = 100_000
HEADERS = {"Accept": "application/json"}

_COMPANY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_ID = re.compile(r"^\d{1,20}$")
_NOT_TENANTS = {"www", "app", "api", "static", "cdn", "docs", "help", "support", "status", "blog"}
_PATHS = ("/careers", "/api/apply", "/api/pcsx")
# "onsite" is often set on postings whose location says Remote, so only remote/hybrid are reported
_REMOTE = {"remote": "yes", "remote_local": "yes", "remote_global": "yes", "hybrid": "hybrid"}
# v2 rows spell the US several ways and PCSX rows say "US"; the country hint always says "US"
_US = {"united states of america", "united states", "usa", "us", "u.s.", "u.s.a."}


def parse_slug(slug: str) -> tuple[str, str]:
    """(company, domain) from a "company:domain" slug; ValueError if either part is malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"bad Eightfold slug {slug!r}: expected company:domain")
    company, domain = parts[0].lower(), parts[1].lower()
    if not (_COMPANY.match(company) and _DOMAIN.match(domain)) or company in _NOT_TENANTS:
        raise ValueError(f"bad Eightfold slug {slug!r}")
    return company, domain


def _base(company: str) -> str:
    """The career site's origin, built only from a validated label under eightfold.ai."""
    return f"https://{quote(company, safe='')}.eightfold.ai"


def _job_url(company: str, domain: str, pid: str) -> str:
    """The human posting page for one position."""
    return f"{_base(company)}/careers/job/{quote(pid, safe='')}?domain={quote(domain, safe='')}"


def _parts(url: str) -> tuple[str, str, list[str]] | None:
    """(company, domain, path segments) of an Eightfold careers link, or None if it isn't one."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    labels = host.split(".")
    if not (host.endswith(".eightfold.ai") and len(labels) == 3):
        return None
    if not any(p.path == x or p.path.startswith(x + "/") for x in _PATHS):
        return None
    domain = (parse_qs(p.query).get("domain") or [""])[0].strip().lower()
    try:
        company, domain = parse_slug(f"{labels[0]}:{domain}")
    except ValueError:
        return None
    return company, domain, [s for s in p.path.split("/") if s]


def board_of(url: str) -> str | None:
    """The "company:domain" slug of an Eightfold careers link, or None for any other link."""
    got = _parts(url)
    return f"{got[0]}:{got[1]}" if got else None


def _status(e: Exception) -> int | None:
    """The HTTP status of an error from appa.sources.http, if it has one."""
    return getattr(getattr(e, "response", None), "status_code", None)


def _forbidden(e: Exception) -> bool:
    """True when an HTTP error is a 403 (the tenant uses the other API)."""
    return _status(e) == 403


def _get(url: str, params: dict):
    """get_json, waiting once and retrying when Eightfold says 429 Too Many Requests."""
    try:
        return get_json(url, params=params, headers=HEADERS)
    except Exception as e:
        if _status(e) != 429:
            raise
        time.sleep(BACKOFF)
        return get_json(url, params=params, headers=HEADERS)


def _day(ts) -> date | None:
    """A date from a Unix timestamp in seconds, or None."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date() if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _company_name(domain: str) -> str:
    """A readable company name from the domain, when the API doesn't give one."""
    return domain.split(".")[0].replace("-", " ").title()


def _countries(locs: list[str]) -> str:
    """The country part of each location ("City, State, Country"), deduped, comma separated."""
    out: list[str] = []
    for loc in locs:
        c = str(loc).rsplit(",", 1)[-1].strip()
        c = "US" if c.lower() in _US else c
        if c and c not in out:
            out.append(c)
    return ", ".join(out)[:300]


def _canonical(row: dict, pid: str) -> str:
    """The company-domain copy of the posting link (canonicalPositionUrl), when it is https and ends in
    /careers/job/{id}."""
    url = str(row.get("canonicalPositionUrl") or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    ok = p.scheme == "https" and p.hostname and p.path.rstrip("/").endswith(f"/careers/job/{pid}") and " " not in url
    return url[:500] if ok else ""


def _listing(row: dict, company: str, domain: str, name: str, pcsx: bool) -> Listing | None:
    """A Listing from one search row (either API's shape), or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    pid, title = str(row.get("id") or ""), str(row.get("name") or row.get("posting_name") or "")
    if not (_ID.match(pid) and title.strip()):
        return None
    locs = [str(x) for x in (row.get("locations") or []) if x] if isinstance(row.get("locations"), list) else []
    std = (
        [str(x) for x in (row.get("standardizedLocations") or []) if x]
        if isinstance(row.get("standardizedLocations"), list)
        else []
    )
    work = str(row.get("workLocationOption" if pcsx else "work_location_option") or "").strip().lower()
    hints = {
        "remote": _REMOTE.get(work, ""),
        "category": str(row.get("department") or ""),
        "country": _countries(std or locs),
        "links": _canonical(row, pid),
    }
    posted = _day(row.get("postedTs") or row.get("creationTs")) if pcsx else _day(row.get("t_create"))
    try:
        return Listing(
            company=name,
            title=title,
            url=_job_url(company, domain, pid),
            location="; ".join(locs)[:500] or str(row.get("location") or "")[:500],
            posted_at=posted,
            ats="eightfold",
            ats_job_id=pid,
            source_ref=str(row.get("ats_job_id") or row.get("atsJobId") or pid)[:300],
            hints=hints,
        )
    except ValueError:
        return None


def _search(company: str, domain: str, query: str, start: int, us: bool, pcsx: bool) -> dict:
    """One search page from the tenant's API."""
    params = {"domain": domain, "start": start, "query": query, "sort_by": "timestamp"}
    if us:
        params["location"] = "United States"
    if pcsx:
        data = _get(f"{_base(company)}/api/pcsx/search", params)
        return (data.get("data") or {}) if isinstance(data, dict) else {}
    data = _get(f"{_base(company)}/api/apply/v2/jobs", {**params, "num": PAGE})
    return data if isinstance(data, dict) else {}


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one Eightfold career site: one search per opts.queries, capped by opts.max_results."""
    opts = opts or FeedOptions()
    company, domain = parse_slug(slug)
    pcsx, name = False, _company_name(domain)
    out: list[Listing] = []
    seen: set[str] = set()
    for query in opts.queries or ("",):
        start, last = 0, None
        for _ in range(MAX_PAGES):
            if len(out) >= opts.max_results:
                return out[: opts.max_results]
            time.sleep(PCSX_PAUSE if pcsx else PAUSE)
            try:
                data = _search(company, domain, query, start, opts.us_only, pcsx)
            except Exception as e:
                if pcsx or not _forbidden(e):
                    raise
                pcsx = True
                time.sleep(PCSX_PAUSE)
                data = _search(company, domain, query, start, opts.us_only, pcsx)
            brand = data.get("branding")
            if isinstance(brand, dict) and str(brand.get("companyName") or "").strip():
                name = str(brand["companyName"]).strip()
            rows = data.get("positions")
            if not rows or not isinstance(rows, list):
                break
            ids = {str(r.get("id")) for r in rows if isinstance(r, dict)}
            if ids == last:  # the API repeated the previous page: this query is done
                break
            last = ids
            fresh = old = 0
            for row in rows:
                x = _listing(row, company, domain, name, pcsx)
                if x is None:
                    continue
                fresh += 1
                if opts.since and x.posted_at and x.posted_at < opts.since:
                    old += 1
                    continue
                if x.url not in seen:  # seen spans queries only to dedupe the output
                    seen.add(x.url)
                    out.append(x)
            start += len(rows)
            count = data.get("count")
            if not fresh or old == fresh or len(rows) < PAGE or (isinstance(count, int) and start >= count):
                break
    return out[: opts.max_results]


def describe(listing: Listing) -> str:
    """The posting's full text, from Eightfold's position detail endpoint (one request, two on new-API tenants)."""
    got = _parts(listing.url)
    if not got or len(got[2]) != 3 or got[2][:2] != ["careers", "job"] or not _ID.match(got[2][2]):
        raise ValueError("not an Eightfold posting link")
    company, domain, segs = got
    pid = segs[2]
    try:
        data = _get(f"{_base(company)}/api/apply/v2/jobs/{quote(pid, safe='')}", {"domain": domain})
        raw = str((data or {}).get("job_description") or "") if isinstance(data, dict) else ""
    except Exception as e:
        if not _forbidden(e):
            raise
        data = _get(f"{_base(company)}/api/pcsx/position_details", {"position_id": pid, "domain": domain})
        info = data.get("data") if isinstance(data, dict) else None
        raw = str((info or {}).get("jobDescription") or "") if isinstance(info, dict) else ""
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="eightfold",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="netflix:netflix.com",
        terms="Public career-site search (no login); robots.txt allows /careers, /api/apply and /api/pcsx. Eightfold "
        "rate-limits fast clients (429), so appa paces 1s per request (2s on the PCSX API), backs off 15s once on a "
        "429, and reads at most 50 pages of 10 per query.",
        hosts=("eightfold.ai",),
    )
)
