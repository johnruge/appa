"""Oracle Recruiting Cloud career sites (Candidate Experience on *.fa.*.oraclecloud.com: JPMorgan Chase,
Oracle itself and many large employers), through the public REST search the site's own page calls:
GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;siteNumber=CX_...,... with no
login needed to view.

A board slug is "pod:region:site" (e.g. eeho:us2:CX_45001); region is empty for hosts without one
(jpmc.fa.oraclecloud.com -> jpmc::CX_1001). Links may name the site by an alias ("jobsearch"); fetch reads the
site's page once to find its CX number. appa runs one search per opts.queries, newest first, and with
opts.us_only applies the board's own "United States" location facet. Search rows carry no description:
describe() reads one posting from the details endpoint. Applying needs a candidate account, so appa hands
those back to the owner; discovery is still worth it.
"""

from __future__ import annotations

import re
import time
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

PAGE = 50
MAX_PAGES = 40  # per query
PAUSE = 0.5  # seconds between requests
MAX_DESC = 100_000
HEADERS = {"Accept": "application/json"}
API = "/hcmRestApi/resources/latest"
DESC_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
    "OrganizationDescriptionStr",
    "CorporateDescriptionStr",
)
COMPANIES = {"jpmc": "JPMorgan Chase", "eeho": "Oracle"}  # pods whose names don't read as the company

_POD = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REGION = re.compile(r"^[a-z0-9]{1,15}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SITE_NO = re.compile(r"^CX(?:_\d{1,10})?$")
_LANG = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_PAGE_SITE_NO = re.compile(r"siteNumber=(CX(?:_\d{1,10})?)\b")
_NOT_KEYWORD = re.compile(r"[^\w +.#&/-]")
_REMOTE = {"remote": "yes", "fully remote": "yes", "hybrid": "hybrid", "on-site": "no", "onsite": "no", "on site": "no"}


def parse_slug(slug: str) -> tuple[str, str, str]:
    """(pod, region, site) from a "pod:region:site" slug (region may be empty); ValueError if malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 3:
        raise ValueError(f"bad Oracle HCM slug {slug!r}: expected pod:region:site")
    pod, region, site = parts[0].lower(), parts[1].lower(), parts[2]
    if not (_POD.match(pod) and (region == "" or _REGION.match(region)) and _SITE.match(site)):
        raise ValueError(f"bad Oracle HCM slug {slug!r}")
    return pod, region, site


def _base(pod: str, region: str) -> str:
    """The site's origin, built only from validated parts under oraclecloud.com."""
    return f"https://{pod}.fa.{region}.oraclecloud.com" if region else f"https://{pod}.fa.oraclecloud.com"


def _parts(url: str) -> tuple[str, str, str, list[str]] | None:
    """(pod, region, site, remaining path segments) of a Candidate Experience link, or None."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    labels = (p.hostname or "").lower().rstrip(".").split(".")
    if len(labels) == 5 and labels[1] == "fa" and labels[3:] == ["oraclecloud", "com"]:
        pod, region = labels[0], labels[2]
    elif len(labels) == 4 and labels[1] == "fa" and labels[2:] == ["oraclecloud", "com"]:
        pod, region = labels[0], ""
    else:
        return None
    segs = [s for s in p.path.split("/") if s]
    if (
        len(segs) < 5
        or segs[0].lower() != "hcmui"
        or segs[1].lower() != "candidateexperience"
        or not _LANG.match(segs[2])
        or segs[3] != "sites"
    ):
        return None
    site = segs[4]
    if not (_POD.match(pod) and (region == "" or _REGION.match(region)) and _SITE.match(site)):
        return None
    return pod, region, site, segs[5:]


def board_of(url: str) -> str | None:
    """The "pod:region:site" slug of an Oracle Candidate Experience link, or None for any other link."""
    got = _parts(url)
    return f"{got[0]}:{got[1]}:{got[2]}" if got else None


def _company(pod: str) -> str:
    """A readable company name from the pod (the API doesn't return one)."""
    return COMPANIES.get(pod) or pod.replace("-", " ").title()


def _site_number(base: str, site: str) -> str:
    """The site's CX number: the slug's own when it is one, else read once from the site's page."""
    if _SITE_NO.match(site):
        return site
    text, _ = get_text(f"{base}/hcmUI/CandidateExperience/en/sites/{quote(site, safe='')}/jobs")
    m = _PAGE_SITE_NO.search(text)
    if not m:
        raise RuntimeError(f"no Oracle site number found for site {site!r}")
    return m.group(1)


def _search(base: str, finder: str, expand: str = "requisitionList.secondaryLocations") -> dict:
    """One findReqs search; returns the result item ({} when the reply has none)."""
    data = get_json(
        f"{base}{API}/recruitingCEJobRequisitions",
        params={"onlyData": "true", "expand": expand, "finder": finder},
        headers=HEADERS,
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def _us_location(base: str, site_no: str) -> int | None:
    """The board's own "United States" location facet id (it differs per tenant), or None."""
    item = _search(base, f"findReqs;siteNumber={site_no},facetsList=LOCATIONS,limit=1", "locationsFacet")
    for f in item.get("locationsFacet") or []:
        if isinstance(f, dict) and str(f.get("Name") or "").strip().lower() == "united states":
            try:
                return int(f.get("Id"))
            except (TypeError, ValueError):
                return None
    return None


def _keyword(query: str) -> str:
    """A search phrase safe inside the finder string (no commas, semicolons or quotes)."""
    return " ".join(_NOT_KEYWORD.sub(" ", query or "").split())[:200]


def _posted(text: str) -> date | None:
    """The PostedDate ("2026-10-30") as a date, or None."""
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _hints(row: dict) -> dict[str, str]:
    """Facts the row states: countries, workplace type, schedule, job family."""
    seconds = [s for s in row.get("secondaryLocations") or [] if isinstance(s, dict)]
    countries = [str(c) for c in [row.get("PrimaryLocationCountry"), *(s.get("CountryCode") for s in seconds)] if c]
    schedule = str(row.get("JobSchedule") or "").strip().lower()
    return {
        "country": ", ".join(dict.fromkeys(countries)),
        "remote": _REMOTE.get(str(row.get("WorkplaceType") or "").strip().lower(), ""),
        "employment_type": re.sub(r"[\s-]+", "_", schedule) if schedule else "",
        "category": str(row.get("JobFamily") or row.get("JobFunction") or ""),
    }


def _listing(row: dict, pod: str, region: str, site: str, site_no: str) -> Listing | None:
    """A Listing from one search row, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    job_id, title = str(row.get("Id") or ""), str(row.get("Title") or "")
    if not (_ID.match(job_id) and title.strip()):
        return None
    seconds = [str(s.get("Name")) for s in row.get("secondaryLocations") or [] if isinstance(s, dict) and s.get("Name")]
    location = "; ".join(x for x in dict.fromkeys([str(row.get("PrimaryLocation") or ""), *seconds]) if x)
    url = f"{_base(pod, region)}/hcmUI/CandidateExperience/en/sites/{quote(site, safe='')}/job/{quote(job_id, safe='')}"
    try:
        return Listing(
            company=_company(pod),
            title=title,
            url=url,
            location=location[:500],
            posted_at=_posted(row.get("PostedDate")),
            ats="oracle_hcm",
            ats_job_id=job_id,
            source_ref=f"{site_no}:{job_id}",
            hints=_hints(row),
        )
    except ValueError:
        return None


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one Oracle career site: one newest-first search per opts.queries, capped."""
    opts = opts or FeedOptions()
    pod, region, site = parse_slug(slug)
    base = _base(pod, region)
    site_no = _site_number(base, site)
    us = _us_location(base, site_no) if opts.us_only else None
    out: list[Listing] = []
    seen: set[str] = set()
    for query in opts.queries or ("",):
        kw = _keyword(query)
        offset = 0
        for _ in range(MAX_PAGES):
            if len(out) >= opts.max_results:
                return out[: opts.max_results]
            time.sleep(PAUSE)
            finder = (
                f"findReqs;siteNumber={site_no}"
                + (f",keyword={kw}" if kw else "")
                + (f",locationId={us}" if us else "")
                + f",limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
            )
            item = _search(base, finder)
            rows = item.get("requisitionList")
            if not rows or not isinstance(rows, list):
                break
            new, older = 0, False
            for row in rows:
                x = _listing(row, pod, region, site, site_no)
                if x is None or x.ats_job_id in seen:
                    continue
                seen.add(x.ats_job_id)
                new += 1
                if opts.since and x.posted_at and x.posted_at < opts.since:
                    older = True  # newest first: the rest of this query is older too
                    continue
                out.append(x)
            total = item.get("TotalJobsCount")
            offset += len(rows)
            if older or not new or len(rows) < PAGE or (isinstance(total, int) and offset >= total):
                break
    return out[: opts.max_results]


def describe(listing: Listing) -> str:
    """The posting's full text, from the details endpoint (one request)."""
    got = _parts(listing.url)
    if not got or len(got[3]) < 2 or got[3][0] != "job" or not _ID.match(got[3][1]):
        raise ValueError("not an Oracle Candidate Experience posting link")
    pod, region, site, rest = got
    ref = (listing.source_ref or "").split(":")
    site_no = ref[0] if len(ref) == 2 and _SITE_NO.match(ref[0]) else _site_number(_base(pod, region), site)
    data = get_json(
        f"{_base(pod, region)}{API}/recruitingCEJobRequisitionDetails",
        headers=HEADERS,
        params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{rest[1]}",siteNumber={site_no}'},
    )
    items = data.get("items") if isinstance(data, dict) else None
    info = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    texts, hidden = [], []
    for k in DESC_FIELDS:
        raw = str(info.get(k) or "")
        if raw.strip():
            text, h = html_to_text(raw)
            texts.append(text)
            hidden += h
    return mark_hidden("\n\n".join(t for t in texts if t), hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="oracle_hcm",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="eeho:us2:CX_45001",
        terms="Oracle Recruiting Cloud's public Candidate Experience REST search (what the career page itself calls; "
        "no login to view). robots.txt is behind Oracle's WAF and could not be read. Be polite: paced at 0.5s "
        "per request, at most 40 pages of 50 per query. Applying needs a candidate account.",
        hosts=("oraclecloud.com",),
    )
)
