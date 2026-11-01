"""Workday career sites (every *.myworkdayjobs.com and myworkdaysite.com board), through the public JSON
search the site's own page uses: POST /wday/cxs/{tenant}/{site}/jobs, no login needed to view.

A board slug is "tenant:wdN:site" (e.g. nvidia:wd5:NVIDIAExternalCareerSite). Big boards list thousands
of postings, so appa runs one search per opts.queries and caps pages; with opts.us_only it applies the
board's own "United States" country facet when the board has one. Search rows carry no description:
describe() reads one posting's text from the detail endpoint. Applying needs a Workday account, so appa
hands those back to the owner; discovery is still worth it.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from urllib.parse import quote, unquote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json, post_json
from appa.sources.text import html_to_text, mark_hidden

PAGE = 20  # Workday's page size limit
MAX_PAGES = 25  # per query
PAUSE = 1.0  # seconds between requests
BACKOFF = 3.0  # seconds before retrying a page of stub rows
MAX_DESC = 100_000
HEADERS = {"Accept": "application/json"}
US_NAMES = ("united states", "united states of america")

_TENANT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_WD = re.compile(r"^wd\d{1,3}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")
_JOB_PATH = re.compile(r"^/job/(?:[A-Za-z0-9._~%-]+/)*[A-Za-z0-9._~%-]+$")
_SEG = re.compile(r"^[A-Za-z0-9._~%-]+$")
_AGO = re.compile(r"(\d+)\+?\s+days?\s+ago", re.I)
_REQ = re.compile(r"_([A-Za-z0-9-]*\d[A-Za-z0-9-]*)/?$")
_REMOTE = {
    "remote": "yes",
    "fully remote": "yes",
    "hybrid": "hybrid",
    "flex": "hybrid",
    "on-site": "no",
    "onsite": "no",
    "on site": "no",
    "office": "no",
}


def parse_slug(slug: str) -> tuple[str, str, str]:
    """(tenant, wdN, site) from a "tenant:wdN:site" slug; ValueError if any part is malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 3:
        raise ValueError(f"bad Workday slug {slug!r}: expected tenant:wdN:site")
    tenant, wd, site = parts[0].lower(), parts[1].lower(), parts[2]
    if not (_TENANT.match(tenant) and _WD.match(wd) and _SITE.match(site)):
        raise ValueError(f"bad Workday slug {slug!r}")
    return tenant, wd, site


def _base(tenant: str, wd: str) -> str:
    """The board's origin, built only from validated parts under myworkdayjobs.com."""
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _api(tenant: str, wd: str, site: str) -> str:
    """The board's JSON API root."""
    return f"{_base(tenant, wd)}/wday/cxs/{quote(tenant, safe='')}/{quote(site, safe='')}"


def _parts(url: str) -> tuple[str, str, str, list[str]] | None:
    """(tenant, wdN, site, remaining path segments) of a Workday link, or None if it isn't one."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    segs = [s for s in p.path.split("/") if s]
    if segs and _LOCALE.match(segs[0]):
        segs = segs[1:]
    labels = host.split(".")
    if host.endswith(".myworkdayjobs.com") and len(labels) == 4 and _WD.match(labels[1]):
        tenant, wd = labels[0], labels[1]
        if len(segs) >= 4 and segs[0] == "wday" and segs[1] == "cxs" and segs[2].lower() == tenant:
            segs = segs[3:]
        if not segs or segs[0] == "wday":
            return None
        site, rest = segs[0], segs[1:]
    elif host.endswith(".myworkdaysite.com") and len(labels) == 3 and _WD.match(labels[0]):
        wd = labels[0]
        if len(segs) >= 4 and segs[0] == "wday" and segs[1] == "cxs":
            segs = ["recruiting", *segs[2:]]
        if len(segs) < 3 or segs[0] != "recruiting":
            return None
        tenant, site, rest = segs[1].lower(), segs[2], segs[3:]
    else:
        return None
    if not (_TENANT.match(tenant) and _SITE.match(site)):
        return None
    return tenant, wd, site, rest


def board_of(url: str) -> str | None:
    """The "tenant:wdN:site" slug of a Workday career-site link, or None for any other link."""
    got = _parts(url)
    return f"{got[0]}:{got[1]}:{got[2]}" if got else None


def _posted(text: str, today: date) -> date | None:
    """A date from Workday's relative "Posted 3 Days Ago" text (None for "30+ Days Ago" or unknown)."""
    t = (text or "").lower()
    if "30+" in t:
        return None
    if "today" in t:
        return today
    if "yesterday" in t:
        return today - timedelta(days=1)
    m = _AGO.search(t)
    return today - timedelta(days=int(m.group(1))) if m else None


def _company(tenant: str) -> str:
    """A readable company name from the tenant (Workday's search doesn't return one)."""
    return tenant.replace("-", " ").title()


def _us_facet(api: str) -> dict:
    """The board's own country facet set to the United States, or {} when the board has none."""
    data = post_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, headers=HEADERS)
    stack = list(data.get("facets") or []) if isinstance(data, dict) else []
    while stack:
        f = stack.pop(0)
        if not isinstance(f, dict):
            continue
        for v in f.get("values") or []:
            if not isinstance(v, dict):
                continue
            if "facetParameter" in v:
                stack.append(v)
            elif str(v.get("descriptor") or "").strip().lower() in US_NAMES and v.get("id") and f.get("facetParameter"):
                return {str(f["facetParameter"]): [str(v["id"])]}
    return {}


def _safe_path(segs: list[str]) -> bool:
    """True if every segment is plain and, once %-decoded, is not "."/".." and has no slash, backslash or space."""

    def ok(x: str) -> bool:
        """One segment passes."""
        d = unquote(x)
        return bool(_SEG.match(x)) and d not in (".", "..") and not re.search(r"[/\\\s]", d)

    return bool(segs) and all(ok(x) for x in segs)


def _is_stub(row: object) -> bool:
    """True for a degraded row that carries only bulletFields (no title or externalPath)."""
    return isinstance(row, dict) and "bulletFields" in row and not row.get("title") and not row.get("externalPath")


def _page(api: str, body: dict) -> tuple[list, int | None]:
    """One search page's (rows, total); retries once after a backoff on stub rows, raises if still mostly stubs."""
    for attempt in range(2):
        time.sleep(PAUSE if attempt == 0 else BACKOFF)
        data = post_json(f"{api}/jobs", body, headers=HEADERS)
        rows = data.get("jobPostings") if isinstance(data, dict) else None
        if not rows or not isinstance(rows, list):
            return [], None
        stubs = sum(_is_stub(r) for r in rows)
        if not stubs:
            break
    if stubs * 2 > len(rows):
        raise RuntimeError("workday returned degraded results (throttled?)")
    total = data.get("total")
    kept = [r for r in rows if not _is_stub(r)] + [None] * stubs  # keep the page size for the short-page check
    return kept, total if isinstance(total, int) and total > 0 else None


def _listing(row: dict, tenant: str, wd: str, site: str, today: date, us: bool) -> Listing | None:
    """A Listing from one search row, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    path, title = str(row.get("externalPath") or ""), str(row.get("title") or "")
    if not (len(path) <= 500 and _JOB_PATH.match(path) and _safe_path(path.split("/")[1:]) and title.strip()):
        return None
    m = _REQ.search(path)
    job_id = (m.group(1) if m else path.rstrip("/").rsplit("/", 1)[-1])[:100]
    hints = {"remote": _REMOTE.get(str(row.get("remoteType") or "").strip().lower(), "")}
    if us:
        hints["country"] = "United States"
    try:
        return Listing(
            company=_company(tenant),
            title=title,
            url=f"{_base(tenant, wd)}/{quote(site, safe='')}{path}",
            location=str(row.get("locationsText") or "")[:500],
            posted_at=_posted(str(row.get("postedOn") or ""), today),
            ats="workday",
            ats_job_id=job_id,
            source_ref=path[:300],
            hints=hints,
        )
    except ValueError:
        return None


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one Workday board: one search per opts.queries, capped by opts.max_results."""
    opts = opts or FeedOptions()
    tenant, wd, site = parse_slug(slug)
    api, today = _api(tenant, wd, site), date.today()
    facets = _us_facet(api) if opts.us_only else {}
    out: list[Listing] = []
    seen: set[str] = set()  # across queries: dedupe only
    for query in opts.queries or ("",):
        offset, qseen, total = 0, set(), None  # qseen (per query) decides when paging stops
        for _ in range(MAX_PAGES):
            if len(out) >= opts.max_results:
                return out[: opts.max_results]
            body = {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": query}
            rows, got = _page(api, body)
            total = got if offset == 0 else total
            if not rows:
                break
            new = 0
            for row in rows:
                x = _listing(row, tenant, wd, site, today, bool(facets))
                if x is None or x.url in qseen:
                    continue
                qseen.add(x.url)
                new += 1
                if x.url in seen:
                    continue
                seen.add(x.url)
                if opts.since and (
                    x.posted_at is None
                    and "30+" in str(row.get("postedOn"))
                    and opts.since > today - timedelta(days=30)
                    or x.posted_at is not None
                    and x.posted_at < opts.since
                ):
                    continue
                out.append(x)
            offset += len(rows)
            if not new or len(rows) < PAGE or (total and offset >= total):
                break
    return out[: opts.max_results]


def describe(listing: Listing) -> str:
    """The posting's full text, from Workday's detail endpoint (one request)."""
    got = _parts(listing.url)
    if not got or not got[3] or got[3][0] != "job":
        raise ValueError("not a Workday posting link")
    tenant, wd, site, rest = got
    if not _safe_path(rest):
        raise ValueError("not a Workday posting link")
    path = "/".join(quote(unquote(s), safe="") for s in rest)
    data = get_json(f"{_api(tenant, wd, site)}/{path}", headers=HEADERS)
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    raw = str((info or {}).get("jobDescription") or "")
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="workday",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="salesforce:wd12:External_Career_Site",
        terms="Public career-site search (no login); robots.txt allows the external site. Be polite: paced at "
        "1s per request, at most 25 pages per query. us_only is best-effort: boards without a country "
        "facet are searched globally. Applying needs a Workday account.",
        hosts=("myworkdayjobs.com",),
    )
)
