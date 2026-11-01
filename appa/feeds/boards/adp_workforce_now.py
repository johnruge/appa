"""ADP Workforce Now career centers, through the public JSON API behind each company's recruitment page (no key).

A company's board is https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}
where cid is a GUID; that GUID is the slug here. The page loads its postings from
GET .../careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&lang=en_US&$top=20&$skip=N
which returns {jobRequisitions[], meta{totalNumber}}: title, post date, locations, work level (Full Time,
Part Time, Intern...), salary range and the public jobId (customFieldGroup ExternalJobID), but no
description and no company name (so the company is the cid). The server returns at most 20 rows a page.
$skip is 1-based: $skip=N (N>=1) returns rows N..N+19, and $skip=0 behaves like $skip=1 minus its last row
(rows 1-19). So we ask for 0, 20, 40, ... and stop only once $skip passes totalNumber. The same path with /{itemID}
returns one requisition with its description HTML, used
by describe(). robots.txt redirects to a login page, so there are no published rules; we page slowly.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from urllib.parse import parse_qs, quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

HOST = "workforcenow.adp.com"
API = f"https://{HOST}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PAGE = f"https://{HOST}/mascsr/default/mdf/recruitment/recruitment.html"
_BOARD_PATHS = ("/mascsr/default/mdf/recruitment/", "/mascsr/default/careercenter/")
_CID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")
_JOBID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")
_TYPES = (
    ("intern", "internship"),
    ("full", "full_time"),
    ("part", "part_time"),
    ("temp", "temporary"),
    ("season", "temporary"),
    ("contract", "contract"),
)
PAGE_SIZE = 20  # the server caps a page at 20 rows
MAX_PAGES = 50
PAUSE = 0.5
MAX_DESC = 100_000


def _check(cid: str) -> str:
    """The cid, lowercased, if it is a GUID; else ValueError."""
    v = cid.strip().lower() if isinstance(cid, str) else ""
    if not _CID.fullmatch(v):
        raise ValueError(f"bad adp workforce now cid: {cid!r}")
    return v


def _params(cid: str) -> str:
    """The query string shared by the list and detail calls."""
    return f"cid={quote(cid, safe='')}&lang=en_US"


def _field(r: dict, group: str, name: str, key: str) -> str:
    """A named value from the requisition's customFieldGroup (e.g. stringFields/ExternalJobID), or ''."""
    for f in (r.get("customFieldGroup") or {}).get(group) or []:
        if isinstance(f, dict) and (f.get("nameCode") or {}).get("codeValue") == name:
            return str(f.get(key) or "").strip()
    return ""


def _day(v) -> date | None:
    """'2026-10-31T00:02:00' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _location(r: dict) -> tuple[str, list[str]]:
    """(location text, country codes) from requisitionLocations ('New York, NY, US')."""
    parts, countries = [], []
    for loc in r.get("requisitionLocations") or []:
        if not isinstance(loc, dict):
            continue
        name = str((loc.get("nameCode") or {}).get("shortName") or "").strip(" ,")
        if not name:
            a = loc.get("address") or {}
            name = ", ".join(
                x
                for x in (
                    str(a.get("cityName") or ""),
                    str((a.get("countrySubdivisionLevel1") or {}).get("codeValue") or ""),
                )
                if x
            )
        if name:
            parts.append(name)
            last = name.rsplit(",", 1)[-1].strip()
            if re.fullmatch(r"[A-Z]{2}", last) and "," in name and last not in countries:
                countries.append(last)
    return "; ".join(dict.fromkeys(parts))[:500], countries


def _hints(r: dict, countries: list[str]) -> dict[str, str]:
    """The facts the requisition states, under HINT_KEYS names."""
    work = str((r.get("workLevelCode") or {}).get("shortName") or "").strip()
    etype = next((v for k, v in _TYPES if k in work.lower()), "")
    visas = [
        str(v.get("shortName") or v.get("codeValue") or "") if isinstance(v, dict) else str(v)
        for v in r.get("sponsoredVisaTypeCodes") or []
    ]
    units = [
        str((u.get("nameCode") or {}).get("shortName") or "")
        for u in r.get("organizationalUnits") or []
        if isinstance(u, dict)
    ]
    return {
        "employment_type": etype,
        "level": "intern" if etype == "internship" else "",
        "salary": _field(r, "stringFields", "SalaryRange", "stringValue"),
        "country": ", ".join(countries),
        "sponsorship": ", ".join(v for v in visas if v),
        "category": ", ".join(u for u in units if u) or _field(r, "stringFields", "HomeDepartment", "stringValue"),
    }


def job_url(cid: str, job_id: str) -> str:
    """The human posting page for a requisition (the board page when there is no public jobId)."""
    base = f"{PAGE}?cid={quote(cid, safe='')}"
    return f"{base}&jobId={quote(job_id, safe='')}&lang=en_US" if job_id else f"{base}&lang=en_US"


def _listing(r: dict, cid: str) -> Listing:
    """One requisition as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(r, dict):
        raise ValueError("not a requisition")
    item = str(r.get("itemID") or "")
    if not _ITEM.fullmatch(item):
        raise ValueError(f"bad itemID {item!r}")
    job_id = _field(r, "stringFields", "ExternalJobID", "stringValue")
    if job_id and not _JOBID.fullmatch(job_id):
        job_id = ""
    location, countries = _location(r)
    posted = _day(r.get("postDate")) or _day(_field(r, "dateFields", "PostingDate", "dateValue"))
    ref = f"{cid}:{item}"
    return Listing(
        company=cid,
        title=str(r.get("requisitionTitle") or ""),
        url=job_url(cid, job_id),
        location=location,
        posted_at=posted,
        ats="adp_workforce_now",
        ats_job_id=ref,
        source_ref=ref,
        hints=_hints(r, countries),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open requisition on this company's Workforce Now career center (no descriptions; see describe)."""
    opts = opts or FeedOptions()
    cid = _check(slug)
    out, seen, raw_seen, skip = [], set(), set(), 0
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        data = get_json(f"{API}?{_params(cid)}&%24top={PAGE_SIZE}&%24skip={skip}")
        rows = (data.get("jobRequisitions") if isinstance(data, dict) else None) or []
        if not rows:
            break
        ids = {str(r.get("itemID")) for r in rows if isinstance(r, dict) and r.get("itemID")}
        fresh = not ids or not ids <= raw_seen  # raw itemIDs, so a malformed page does not stop paging
        raw_seen |= ids
        for r in rows:
            try:
                x = _listing(r, cid)
            except ValueError as e:
                log.debug("skipping adp requisition on %s: %s", cid, e)
                continue
            if x.ats_job_id in seen:
                continue
            seen.add(x.ats_job_id)
            if opts.since and x.posted_at and x.posted_at < opts.since:
                continue
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        skip += PAGE_SIZE
        try:
            total = int((data.get("meta") or {}).get("totalNumber") or 0)
        except (TypeError, ValueError):
            total = 0
        if not fresh or (total and skip > total):
            break  # $skip is 1-based (row N at $skip=N); repeats mean the server ignored $skip
    return out


def describe(listing: Listing) -> str:
    """The requisition's description text, from the public single-requisition endpoint (one request)."""
    cid, _, item = (listing.ats_job_id or "").partition(":")
    cid = _check(cid)
    if not _ITEM.fullmatch(item):
        raise ValueError(f"bad itemID {item!r}")
    data = get_json(f"{API}/{quote(item, safe='')}?{_params(cid)}")
    raw = str((data.get("requisitionDescription") if isinstance(data, dict) else "") or "")
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The cid of a workforcenow.adp.com recruitment / career center link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if host != HOST or not p.path.lower().startswith(_BOARD_PATHS):
        return None
    qs = {k.lower(): v for k, v in parse_qs(p.query).items()}
    try:
        return _check((qs.get("cid") or [""])[0])
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="adp_workforce_now",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="89da4960-4d45-4b46-b7aa-5959c5f71827",
        terms="Public JSON behind each company's ADP career center (no key, no published robots rules); "
        "20 rows a page, paced at 0.5s.",
        hosts=(HOST,),
    )
)
