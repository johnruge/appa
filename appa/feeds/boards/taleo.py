"""Oracle Taleo Enterprise career sections (https://{tenant}.taleo.net/careersection/{section}/...), through
the public JSON search the section's own page uses: POST /careersection/rest/jobboard/searchjobs, no login.

A board slug is "tenant:section" (e.g. hdr:ex; some tenants live on shared hosts like fa009). The search
needs the section's numeric portal id, which appa reads from the section's jobsearch.ftl page (along with
the company name from its title). Sections that need sign-in, or old ones without the faceted search, have
no portal id and raise. Rows are positional columns (title, locations as a JSON string, posted date); they
carry no description, so describe() reads it from the posting page. Applying needs a Taleo account.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import date, datetime
from urllib.parse import parse_qs, quote, unquote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text, post_json
from appa.sources.text import html_to_text, mark_hidden

MAX_PAGES = 40  # per query (25 rows a page)
PAUSE = 0.5  # seconds between requests
MAX_DESC = 100_000
HEADERS = {"Content-Type": "application/json", "tz": "GMT-04:00"}
PAGES = ("jobsearch.ftl", "jobdetail.ftl", "moresearch.ftl")
NOT_TENANTS = ("www", "tbe")  # tbe.taleo.net is Taleo Business Edition, a different product
US_NAMES = ("united states", "united states of america", "usa", "us")
DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%b %d %Y")

_TENANT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PORTAL = re.compile(r"portal(?:=|No:\s*')(\d{1,20})")
_TITLE = re.compile(r"<title>([^<]{0,300})</title>", re.I)
_HISTORY = re.compile(r'id="initialHistory"\s+value="([^"]*)"')
_GENERIC = re.compile(
    r"\b(?:job search|basic search|advanced search|search jobs|job description|find a career|"
    r"career section|careers?|jobs|taleo|sign in|home)\b",
    re.I,
)
_SHARED_HOST = re.compile(r"^[a-z]{2}\d{3}$")  # fa009, aa010...


def parse_slug(slug: str) -> tuple[str, str]:
    """(tenant, section) from a "tenant:section" slug; ValueError if either part is malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"bad Taleo slug {slug!r}: expected tenant:section")
    tenant, section = parts[0].lower(), parts[1]
    if not (_TENANT.match(tenant) and _SECTION.match(section)) or tenant in NOT_TENANTS:
        raise ValueError(f"bad Taleo slug {slug!r}")
    return tenant, section


def _base(tenant: str) -> str:
    """The tenant's origin, built only from a validated label under taleo.net."""
    return f"https://{tenant}.taleo.net"


def _section_url(tenant: str, section: str, page: str) -> str:
    """A page of the career section (jobsearch.ftl, jobdetail.ftl...)."""
    return f"{_base(tenant)}/careersection/{quote(section, safe='')}/{page}"


def _parts(url: str) -> tuple[str, str, str, dict] | None:
    """(tenant, section, page, query) of a Taleo career-section link, or None if it isn't one."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    labels = host.split(".")
    if len(labels) != 3 or labels[1:] != ["taleo", "net"]:
        return None
    tenant = labels[0]
    segs = [s for s in p.path.split("/") if s]
    if len(segs) != 3 or segs[0] != "careersection" or segs[2].lower() not in PAGES:
        return None
    section = segs[1]
    if not (_TENANT.match(tenant) and _SECTION.match(section)) or tenant in NOT_TENANTS or section == "rest":
        return None
    return tenant, section, segs[2].lower(), parse_qs(p.query)


def board_of(url: str) -> str | None:
    """The "tenant:section" slug of a Taleo career-section link, or None for any other link."""
    got = _parts(url)
    return f"{got[0]}:{got[1]}" if got else None


def _company(title: str, tenant: str) -> str:
    """The company name from the section's page title, else from the tenant."""
    for part in re.split(r"\s[|\-–—:]\s|\|", _GENERIC.sub(" ", html.unescape(title))):
        part = " ".join(part.split()).strip(" -|–—:,")
        if len(part) > 1:
            return part[:300]
    return f"{tenant}.taleo.net" if _SHARED_HOST.match(tenant) else tenant.replace("-", " ").title()


def _portal(tenant: str, section: str) -> tuple[str, str]:
    """(portal id, company) from the section's search page; RuntimeError when it has no public search."""
    page, _ = get_text(_section_url(tenant, section, "jobsearch.ftl"), params={"lang": "en"})
    m = _PORTAL.search(page)
    if not m:
        raise RuntimeError(f"taleo section {tenant}:{section} has no public job search (sign-in or legacy)")
    t = _TITLE.findall(page)
    return m.group(1), _company(t[-1] if t else "", tenant)


def _body(query: str, page: int, filters: list) -> dict:
    """The search body the career section's own page sends."""
    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": query, "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": filters},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
        "pageNo": page,
    }


def _search(tenant: str, portal: str, body: dict) -> dict:
    """One search page (paced)."""
    time.sleep(PAUSE)
    data = post_json(
        f"{_base(tenant)}/careersection/rest/jobboard/searchjobs",
        body,
        params={"lang": "en", "portal": portal},
        headers=HEADERS,
    )
    if not isinstance(data, dict):
        return {}
    if data.get("careerSectionUnAvailable") is True:
        raise RuntimeError(f"taleo career section on {tenant} is unavailable")
    return data


def _us_filter(data: dict) -> list:
    """The section's own "United States" location filter from a search's facets, or [] when it has none."""
    for f in data.get("facetResults") or []:
        if not isinstance(f, dict) or f.get("id") != "LOCATION":
            continue
        for v in f.get("facetValueResults") or []:
            if isinstance(v, dict) and str(v.get("text") or "").strip().lower() in US_NAMES and v.get("id"):
                return [{"id": "LOCATION", "selectedValues": [str(v["id"])]}]
    return []


def _date(text: str) -> date | None:
    """A posted date in one of Taleo's display formats, or None."""
    t = " ".join(str(text or "").split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _locations(raw: object) -> str:
    """The row's locations (a JSON-encoded list) as one line."""
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw or "")[:500]
    if isinstance(got, list):
        return "; ".join(str(x) for x in got if x)[:500]
    return str(got or "")[:500]


def _listing(row: object, tenant: str, section: str, company: str, us: bool) -> Listing | None:
    """A Listing from one search row, or None if it's malformed."""
    if not isinstance(row, dict) or not isinstance(row.get("column"), list):
        return None
    cols, job = row["column"], str(row.get("contestNo") or "").strip()
    linked = row.get("linkedColumn") if isinstance(row.get("linkedColumn"), int) else 0
    locs = [i for i in row.get("locationsColumns") or [] if isinstance(i, int) and 0 <= i < len(cols)]
    if not (_JOB.match(job) and 0 <= linked < len(cols)):
        return None
    posted = next((d for i, c in enumerate(cols) if i != linked and i not in locs and (d := _date(c))), None)
    try:
        return Listing(
            company=company,
            title=str(cols[linked] or ""),
            location=_locations(cols[locs[0]]) if locs else "",
            url=f"{_section_url(tenant, section, 'jobdetail.ftl')}?job={quote(job, safe='')}&lang=en",
            posted_at=posted,
            ats="taleo",
            ats_job_id=job,
            source_ref=str(row.get("jobId") or "")[:300] or None,
            hints={"country": "United States"} if us else {},
        )
    except ValueError:
        return None


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one Taleo career section: one keyword search per opts.queries, capped by opts.max_results."""
    opts = opts or FeedOptions()
    tenant, section = parse_slug(slug)
    portal, company = _portal(tenant, section)
    filters = _us_filter(_search(tenant, portal, _body("", 1, []))) if opts.us_only else []
    out: list[Listing] = []
    seen: set[str] = set()  # contestNo: the tenant's public requisition number
    for query in opts.queries or ("",):
        qseen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            if len(out) >= opts.max_results:
                return out[: opts.max_results]
            data = _search(tenant, portal, _body(query, page, filters))
            rows = data.get("requisitionList")
            if not rows or not isinstance(rows, list):
                break
            new = 0
            for row in rows:
                x = _listing(row, tenant, section, company, bool(filters))
                if x is None or x.ats_job_id in qseen:
                    continue
                qseen.add(x.ats_job_id)
                new += 1
                if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
                    continue
                seen.add(x.ats_job_id)
                out.append(x)
            paging = data.get("pagingData") if isinstance(data.get("pagingData"), dict) else {}
            size, total = paging.get("pageSize"), paging.get("totalCount")
            if not new or not (isinstance(size, int) and isinstance(total, int)) or page * size >= total:
                break
    return out[: opts.max_results]


def _decode(part: str) -> str:
    """One field of Taleo's page-state string: %-encoded, with backslash escapes."""
    return re.sub(r"\\(.)", r"\1", unquote(part))


def describe(listing: Listing) -> str:
    """The posting's full text, from its jobdetail.ftl page (one request)."""
    got = _parts(listing.url)
    job = (got[3].get("job") or [""])[0] if got else ""
    if not got or got[2] != "jobdetail.ftl" or not _JOB.match(job):
        raise ValueError("not a Taleo posting link")
    tenant, section = got[0], got[1]
    page, _ = get_text(_section_url(tenant, section, "jobdetail.ftl"), params={"job": job, "lang": "en"})
    m = _HISTORY.search(page)
    if not m:
        return ""
    parts = html.unescape(m.group(1)).split("!|!")
    start = next((i for i, p in enumerate(parts) if p.startswith("!*!")), None)
    if start is None:
        return ""
    chunks = []
    for p in parts[start:]:
        if not p:
            break
        chunks.append(_decode(p.removeprefix("!*!")))
    text, hidden = html_to_text("\n".join(chunks))
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="taleo",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="hdr:ex",
        terms="Public career-section search (no login); robots.txt is not served. Be polite: paced at 0.5s per "
        "request, at most 40 pages per query. Sections behind sign-in are skipped. Applying needs a Taleo account.",
        hosts=("taleo.net",),
    )
)
