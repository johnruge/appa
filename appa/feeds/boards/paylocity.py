"""Paylocity Recruiting job boards, read from each company's public careers page (no key).

GET https://recruiting.paylocity.com/Recruiting/Jobs/All/{guid} is the careers page a company links to; it
embeds every open job in one `window.pageData = {...};` object (no pagination): JobId, JobTitle,
LocationName (often an internal site label such as "ICF 1005"), PublishedDate, a short teaser, IsInternal,
IsRemote, ShouldDisplayLocation, HiringDepartment and JobLocation (City, State and an ISO alpha-3 country
code, mapped to a country name for the filter). ModuleTitle is the company name. Some tenants live on a numbered host
such as
2000recruiting.paylocity.com, where job IDs are separate, so the slug is the guid for the main host and
"{host label}:{guid}" (e.g. "2000recruiting:e2bcef5a-...") for the others. A retired guid redirects to
/Recruiting/Jobs/JobNotFound. Each posting's page is /Recruiting/Jobs/Details/{JobId}; describe() reads
its full text from the page's schema.org JobPosting block. The documented JSON feed
(/recruiting/v2/api/feed/jobs/{guid}) is empty unless an employer turns it on, so it isn't used.
robots.txt is a 404 (no rules).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

DOMAIN = "paylocity.com"
MAIN = "recruiting"  # recruiting.paylocity.com
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LABEL = re.compile(r"^[0-9]{1,6}recruiting$")  # other tenants' hosts, e.g. 2000recruiting
_JOB_ID = re.compile(r"^[0-9]{1,15}$")
_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
MAX_DESC = 100_000
COUNTRIES = {  # JobLocation.Country codes seen on boards
    "USA": "United States",
    "CAN": "Canada",
    "MEX": "Mexico",
    "IND": "India",
    "PHL": "Philippines",
    "GBR": "United Kingdom",
    "CZE": "Czech Republic",
    "IRL": "Ireland",
    "DEU": "Germany",
    "FRA": "France",
    "NLD": "Netherlands",
    "ESP": "Spain",
    "PRT": "Portugal",
    "ITA": "Italy",
    "POL": "Poland",
    "ROU": "Romania",
    "UKR": "Ukraine",
    "ISR": "Israel",
    "BRA": "Brazil",
    "COL": "Colombia",
    "ARG": "Argentina",
    "CHL": "Chile",
    "PER": "Peru",
    "AUS": "Australia",
    "NZL": "New Zealand",
    "CHN": "China",
    "JPN": "Japan",
    "KOR": "Korea",
    "SGP": "Singapore",
    "MYS": "Malaysia",
    "PAK": "Pakistan",
    "ZAF": "South Africa",
    "NGA": "Nigeria",
    "KEN": "Kenya",
    "ARE": "UAE",
    "EGY": "Egypt",
}


def _check(slug: str) -> tuple[str, str]:
    """(host label, guid) of a safe slug ("guid" or "label:guid"); else ValueError."""
    s = slug.strip().lower() if isinstance(slug, str) else ""
    label, _, guid = s.rpartition(":")
    label = label or MAIN
    if not _GUID.fullmatch(guid) or not (label == MAIN or _LABEL.fullmatch(label)):
        raise ValueError(f"bad paylocity slug: {slug!r}")
    return label, guid


def _slug(label: str, guid: str) -> str:
    """The canonical slug: the bare guid on the main host, else "label:guid"."""
    return guid if label == MAIN else f"{label}:{guid}"


def board_url(slug: str) -> str:
    """The company's careers page URL."""
    label, guid = _check(slug)
    return f"https://{label}.{DOMAIN}/Recruiting/Jobs/All/{quote(guid, safe='')}"


def job_url(label: str, job_id: str) -> str:
    """The human posting page for a job on a host."""
    return f"https://{label}.{DOMAIN}/Recruiting/Jobs/Details/{quote(job_id, safe='')}"


def _page_data(html: str) -> dict:
    """The `window.pageData` object embedded in a careers page ({} when missing or broken)."""
    i = html.find("pageData = ")
    if i < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html, i + len("pageData = "))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _day(v) -> date | None:
    """'2026-10-30T14:59:16-05:00' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _country(code) -> str:
    """Paylocity's ISO alpha-3 country code as a name appa's location filter knows (unknown codes unchanged)."""
    c = str(code or "").strip()
    return COUNTRIES.get(c.upper(), c)


def _location(j: dict) -> str:
    """Where the job is: "City, State" from JobLocation, since LocationName is often a site label ("ICF 1005").

    LocationName wins when it already reads like a place ("Pittsford, NY", "Remote, Philippines") and is the
    fallback when there is no city or state; "" when the employer hides the location; "Remote" is appended
    for remote jobs that don't say so."""
    if j.get("ShouldDisplayLocation") is False:
        return ""
    loc = j.get("JobLocation") if isinstance(j.get("JobLocation"), dict) else {}
    name = str(j.get("LocationName") or loc.get("Name") or "").strip()
    city_state = ", ".join(str(loc.get(k) or "").strip() for k in ("City", "State") if str(loc.get(k) or "").strip())
    text = name if "," in name or not city_state else city_state
    if j.get("IsRemote") is True and "remote" not in text.lower():
        text = f"{text}, Remote" if text else "Remote"
    return text[:500]


def _hints(j: dict) -> dict[str, str]:
    """Facts the page states: remote (only when it says so), country, department."""
    loc = j.get("JobLocation") if isinstance(j.get("JobLocation"), dict) else {}
    return {
        "remote": "yes" if j.get("IsRemote") is True else "",
        "country": _country(loc.get("Country")),
        "category": str(j.get("HiringDepartment") or ""),
    }


def _listing(j: dict, label: str, company: str) -> Listing:
    """One job as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict):
        raise ValueError("not a job")
    jid = str(j.get("JobId") or "")
    if not _JOB_ID.fullmatch(jid):
        raise ValueError(f"bad job id: {jid[:50]!r}")
    return Listing(
        company=company,
        title=str(j.get("JobTitle") or ""),
        url=job_url(label, jid),
        location=_location(j),
        posted_at=_day(j.get("PublishedDate")),
        ats="paylocity",
        ats_job_id=jid,
        source_ref=jid,
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open public job on this company's Paylocity board (one request; no descriptions, see describe)."""
    opts = opts or FeedOptions()
    label, guid = _check(slug)
    html, final = get_text(board_url(slug))
    if "/jobnotfound" in urlparse(final).path.lower():
        raise RuntimeError(
            f"404: paylocity board {_slug(label, guid)} redirects to JobNotFound (retired or wrong guid)"
        )
    data = _page_data(html)
    company = str(data.get("ModuleTitle") or "").strip() or _slug(label, guid)
    out, seen = [], set()
    for j in data.get("Jobs") if isinstance(data.get("Jobs"), list) else []:
        if isinstance(j, dict) and j.get("IsInternal"):
            continue
        try:
            x = _listing(j, label, company)
        except ValueError as e:
            log.debug("skipping paylocity job on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def _job_ref(url: str) -> tuple[str, str] | None:
    """(host label, job id) of a Paylocity Details link, or None."""
    host, parts = _split(url)
    if not host or len(parts) < 4 or parts[:3] != ["recruiting", "jobs", "details"] or not _JOB_ID.fullmatch(parts[3]):
        return None
    return host, parts[3]


def _text(html: str) -> str:
    """The posting's text from a Details page: the JobPosting block (not on every page), else the details section."""
    raw = ""
    for m in _LD.finditer(html):
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") == "JobPosting" and isinstance(ld.get("description"), str):
            raw = ld["description"]
            break
    if not raw:
        div = BeautifulSoup(html, "html.parser").find("div", class_="job-preview-details")
        for btn in div.find_all("a", class_="apply-button") if div else []:
            btn.decompose()
        raw = str(div) if div else ""
    if not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def describe(listing: Listing) -> str:
    """The full posting text from its Details page (one request; "" when the job is gone)."""
    ref = _job_ref(listing.url)
    if not ref:
        return ""
    html, final = get_text(job_url(*ref))
    if "/jobnotfound" in urlparse(final).path.lower():
        return ""
    return _text(html)


def _split(url: str) -> tuple[str, list[str]]:
    """(host label, lowercased path parts) of a link on a Paylocity recruiting host, else ("", [])."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return "", []
    if p.scheme not in ("http", "https") or not host.endswith("." + DOMAIN):
        return "", []
    label = host[: -len(DOMAIN) - 1]
    if not (label == MAIN or _LABEL.fullmatch(label)):
        return "", []
    return label, [x.lower() for x in (p.path or "").split("/") if x]


def board_of(url: str) -> str | None:
    """The board slug in a Paylocity careers-page link (/Recruiting/Jobs/All/{guid}), or None.

    Details/{id} links don't carry the guid, so they can't be mapped to a board without a request."""
    label, parts = _split(url)
    if not label or len(parts) < 4 or parts[:3] != ["recruiting", "jobs", "all"] or not _GUID.fullmatch(parts[3]):
        return None
    return _slug(label, parts[3])


BOARD = register_board(
    BoardFeed(
        ats="paylocity",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="e9d022bb-1790-488d-a784-e537639793d5",
        terms="Public careers page HTML (no key; robots.txt is a 404); one request per board, one per described job.",
        hosts=("recruiting.paylocity.com", "2000recruiting.paylocity.com"),
    )
)
