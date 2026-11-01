"""UKG Pro Recruiting (formerly UltiPro) job boards: recruiting.ultipro.com, recruiting2.ultipro.com,
recruiting.ultipro.ca and other *.ultipro.com / *.ultipro.ca hosts.

A board lives at https://{host}/{TENANT}/JobBoard/{board guid}/. The public board page loads its postings
with a keyless JSON POST to .../JobBoardView/LoadSearchResults ({opportunities[], totalCount}, paged by
Top/Skip, newest first). Tenants exist on one host only, so the slug keeps it: "host:tenant:guid" (tenant
and guid lowercased; UKG treats them case-insensitively), e.g.
"recruiting.ultipro.com:rop1001roper:cf088029-453b-46f8-bcab-46ffb10fdf54".

Search rows carry only a short summary, so description stays "" and describe() reads the full text from
the posting page (OpportunityDetail, which robots.txt allows). The search gives no company name: appa reads
the board's logo caption from one posting page per fetch, else falls back to the tenant code.

Terms: robots.txt is "Disallow: /", "Allow: */JobBoard/", "Disallow: */JobBoardView", so the search XHR the
board page itself calls is robots-disallowed for crawlers. appa is not a crawler: it only polls boards it
found through job links, newest-first, 100 rows per request, paced at 1s, and stops at opts.since.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import date, datetime
from urllib.parse import parse_qs, quote, urlparse

import httpx

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text, post_json
from appa.sources.text import html_to_text, mark_hidden

PAGE = 100  # the board UI's largest page size
MAX_PAGES = 20
PAUSE = 1.0  # seconds between requests
MAX_DESC = 100_000
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}
DOMAINS = ("ultipro.com", "ultipro.ca")
REMOTE = {1: "no", 2: "yes"}  # JobLocationType per UKG's own UI code (0 = hybrid, but it's also the default)

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TENANT = re.compile(r"^[a-z0-9]{2,40}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOGO = re.compile(r'<img[^>]*class="[^"]*\blogo\b[^"]*"[^>]*\balt="([^"]{1,200})"', re.I)
_DETAIL = "US.Opportunity.CandidateOpportunityDetail("
_FILTERS = [{"t": "TermsSearchFilterDto", "fieldName": n, "extra": None, "values": []} for n in (4, 5, 6)]
_MATCH = {
    "PreferredJobs": [],
    "Educations": [],
    "LicenseAndCertifications": [],
    "Skills": [],
    "hasNoLicenses": False,
    "SkippedSkills": [],
}
_companies: dict[str, str] = {}  # slug -> company name, found once per process


def _host_ok(host: str) -> bool:
    """True for a single-label subdomain of ultipro.com or ultipro.ca (e.g. recruiting2.ultipro.com)."""
    labels = host.split(".")
    return len(labels) == 3 and ".".join(labels[1:]) in DOMAINS and bool(_LABEL.match(labels[0]))


def parse_slug(slug: str) -> tuple[str, str, str]:
    """(host, tenant, board guid) from a "host:tenant:guid" slug; ValueError if any part is malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 3:
        raise ValueError(f"bad UKG slug {slug!r}: expected host:tenant:guid")
    host, tenant, guid = (p.lower() for p in parts)
    if not (_host_ok(host) and _TENANT.match(tenant) and _GUID.match(guid)):
        raise ValueError(f"bad UKG slug {slug!r}")
    return host, tenant, guid


def _board(host: str, tenant: str, guid: str) -> str:
    """The board's root URL, built only from validated parts under an UltiPro domain."""
    return f"https://{host}/{quote(tenant, safe='')}/JobBoard/{quote(guid, safe='')}"


def _parts(url: str) -> tuple[str, str, str, list[str], str] | None:
    """(host, tenant, guid, path after the guid, query) of an UltiPro job-board link, or None."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme not in ("http", "https") or not _host_ok(host):
        return None
    segs = [s for s in p.path.split("/") if s]
    if len(segs) < 3 or segs[1].lower() != "jobboard":
        return None
    tenant, guid = segs[0].lower(), segs[2].lower()
    if not (_TENANT.match(tenant) and _GUID.match(guid)):
        return None
    return host, tenant, guid, segs[3:], p.query


def board_of(url: str) -> str | None:
    """The "host:tenant:guid" slug of an UltiPro job-board link, or None for any other link."""
    got = _parts(url)
    return f"{got[0]}:{got[1]}:{got[2]}" if got else None


def _date(v: object) -> date | None:
    """A date from an ISO timestamp like 2026-10-28T14:03:11.5Z, or None."""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _where(locs: object) -> tuple[str, str]:
    """(location text, comma-separated countries) from a row's Locations."""
    places, countries = [], []
    for loc in locs if isinstance(locs, list) else []:
        a = (loc or {}).get("Address") if isinstance(loc, dict) else None
        if not isinstance(a, dict):
            continue
        state, country = a.get("State") or {}, a.get("Country") or {}
        bits = [a.get("City"), state.get("Code") or state.get("Name"), country.get("Code")]
        text = ", ".join(str(b) for b in bits if b)
        if text and text not in places:
            places.append(text)
        if country.get("Name") and country["Name"] not in countries:
            countries.append(str(country["Name"]))
    return "; ".join(places)[:500], ", ".join(countries)


def _detail_url(host: str, tenant: str, guid: str, job_id: str) -> str:
    """The posting page. jobid= repeats the ID so appa's link-based dedupe keys on the posting, not the board."""
    q = quote(job_id, safe="")
    return f"{_board(host, tenant, guid)}/OpportunityDetail?opportunityId={q}&jobid={q}"


def _listing(row: object, host: str, tenant: str, guid: str, company: str) -> Listing | None:
    """A Listing from one search row, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    job_id, title = str(row.get("Id") or "").lower(), str(row.get("Title") or "")
    if not (_GUID.match(job_id) and title.strip()):
        return None
    location, countries = _where(row.get("Locations"))
    full = row.get("FullTime")
    hints = {
        "country": countries,
        "remote": REMOTE.get(row.get("JobLocationType"), ""),
        "category": str(row.get("JobCategoryName") or ""),
        "employment_type": {True: "full_time", False: "part_time"}.get(full, ""),
    }
    try:
        return Listing(
            company=company,
            title=title,
            url=_detail_url(host, tenant, guid, job_id),
            location=location,
            posted_at=_date(row.get("PostedDate")),
            ats="ukg_ultipro",
            ats_job_id=job_id,
            source_ref=str(row.get("RequisitionNumber") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def _company(slug: str, host: str, tenant: str, guid: str, job_id: str | None) -> str:
    """The board's brand (its logo caption on a posting page), else the tenant code; looked up once per slug."""
    if slug in _companies:
        return _companies[slug]
    name = ""
    if job_id:
        try:
            page, _ = get_text(_detail_url(host, tenant, guid, job_id))
            m = _LOGO.search(page)
            name = " ".join(html.unescape(m.group(1)).split()) if m else ""
        except (httpx.HTTPError, ValueError):
            name = ""
    if not name:
        m = re.search(r"\d([a-z]{2,})$", tenant)
        name = (m.group(1) if m else tenant).upper()
    _companies[slug] = name
    return name


def _body(skip: int) -> dict:
    """The search request the board page itself sends, newest first."""
    order = [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]
    return {
        "opportunitySearch": {"Top": PAGE, "Skip": skip, "QueryString": "", "OrderBy": order, "Filters": _FILTERS},
        "matchCriteria": _MATCH,
    }


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one UKG board, newest first, until totalCount, opts.since or opts.max_results."""
    opts = opts or FeedOptions()
    host, tenant, guid = parse_slug(slug)
    slug = f"{host}:{tenant}:{guid}"
    api = f"{_board(host, tenant, guid)}/JobBoardView/LoadSearchResults"
    out: list[Listing] = []
    seen: set[str] = set()
    company = None
    skip = 0
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        data = post_json(api, _body(skip), headers=HEADERS)
        rows = data.get("opportunities") if isinstance(data, dict) else None
        if not rows or not isinstance(rows, list):
            break
        if company is None:
            first = next(
                (
                    str(r.get("Id")).lower()
                    for r in rows
                    if isinstance(r, dict) and _GUID.match(str(r.get("Id") or "").lower())
                ),
                None,
            )
            company = _company(slug, host, tenant, guid, first)
        old = False
        for row in rows:
            x = _listing(row, host, tenant, guid, company)
            if x is None or x.ats_job_id in seen:
                continue
            seen.add(x.ats_job_id)
            if opts.since and x.posted_at and x.posted_at < opts.since:
                old = True
                continue
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        skip += len(rows)
        total = data.get("totalCount")
        if old or len(rows) < PAGE or not isinstance(total, int) or skip >= total:
            break
    return out


def describe(listing: Listing) -> str:
    """The posting's full text, from its OpportunityDetail page (one request)."""
    got = _parts(listing.url)
    if not got or [s.lower() for s in got[3]] != ["opportunitydetail"]:
        raise ValueError("not a UKG posting link")
    host, tenant, guid, _, query = got
    job_id = (parse_qs(query).get("opportunityId") or [""])[0].lower()
    if not _GUID.match(job_id):
        raise ValueError("not a UKG posting link")
    page, _ = get_text(_detail_url(host, tenant, guid, job_id))
    i = page.find(_DETAIL)
    if i < 0:
        return ""
    try:
        info, _ = json.JSONDecoder().raw_decode(page, i + len(_DETAIL))
    except ValueError:
        return ""
    if not isinstance(info, dict):
        return ""
    raw = "\n".join(str(info.get(k) or "") for k in ("Description", "PayTransparencyPolicyStatement"))
    if not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="ukg_ultipro",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="recruiting.ultipro.com:rop1001roper:cf088029-453b-46f8-bcab-46ffb10fdf54",
        terms="Public UKG Pro Recruiting board (no login). robots.txt allows */JobBoard/ pages but disallows "
        "*/JobBoardView, the search XHR the board page itself calls; appa only polls boards found through "
        "job links, 100 rows per request, paced at 1s, newest first, stopping at opts.since. Applying may "
        "need a candidate account on the board.",
        hosts=DOMAINS,
    )
)
