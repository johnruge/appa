"""USAJOBS Search API: every open US federal civilian job announcement, from OPM's official API
(https://developer.usajobs.gov). Needs a free API key (USAJOBS_API_KEY) and the email it was
registered with (USAJOBS_EMAIL, sent as the User-Agent, as the API requires). appa runs one search
per query, up to 500 rows a page and 10,000 rows a query (the API's caps). Most announcements are
open to U.S. citizens only, so the source is off by default and flags citizenship_required.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://data.usajobs.gov/api/search"
PAGE_SIZE = 500  # the API's maximum rows per page
MAX_PAGES = 20  # 500 x 20 = the API's 10,000-row cap per query
MAX_DAYS = 60  # DatePosted accepts 0-60 days
PAUSE = 0.5  # seconds between page requests
US = ("united states", "us", "usa")
# "U.S. citizen(s)", "citizens of the United States", "citizenship is required", but not "non-U.S. citizens".
_CITIZEN = re.compile(
    r"(?<!non-)(?<!non )\b(?:u\.\s?s\.|us|united states) citizen|(?<!non-)(?<!non )\bcitizens? of "
    r"the united states|citizenship is required"
)


def _headers() -> dict:
    """The three headers the API requires; raises RuntimeError when the key or email is missing."""
    key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key:
        raise RuntimeError("USAJOBS_API_KEY is not set")
    if not email:
        raise RuntimeError("USAJOBS_EMAIL is not set")
    return {"User-Agent": email, "Authorization-Key": key}


def _dict(v) -> dict:
    """v if it is a dict, else an empty dict (malformed rows must not crash the fetch)."""
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    """v if it is a list, else an empty list."""
    return v if isinstance(v, list) else []


def _date(s) -> date | None:
    """An ISO timestamp from the API as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(s)[:19]).date()
    except (TypeError, ValueError):
        return None


def _text(v) -> str:
    """A details field (string or list of strings, possibly HTML) as plain text."""
    parts = v if isinstance(v, list) else [v]
    return "\n".join(str(p) for p in parts if p)


def _description(d: dict, details: dict) -> str:
    """The posting text from the summary, duties, qualifications and requirements, with hidden text flagged."""
    sections = [
        ("Summary", details.get("JobSummary")),
        ("Who may apply", _dict(details.get("WhoMayApply")).get("Name")),
        ("Duties", details.get("MajorDuties")),
        ("Qualifications", d.get("QualificationSummary")),
        ("Requirements", details.get("KeyRequirements")),
        ("Conditions", details.get("Requirements")),
        ("Education", details.get("Education")),
        ("Other information", details.get("OtherInformation")),
    ]
    raw = "".join(f"<h3>{label}</h3><div>{_text(v)}</div>" for label, v in sections if _text(v))
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:100_000]


def _salary(d: dict) -> str:
    """The posted pay range, e.g. '$92108 - $119746 Per Year'."""
    pay = _dict((_list(d.get("PositionRemuneration")) or [{}])[0])
    lo, hi, per = pay.get("MinimumRange"), pay.get("MaximumRange"), pay.get("Description") or ""
    if not (lo or hi):
        return ""
    return f"${lo} - ${hi} {per}".strip() if lo and hi else f"${lo or hi} {per}".strip()


def _listing(item: dict, us_only: bool) -> Listing | None:
    """A Listing from one search result, or None for malformed rows (or non-US ones when us_only)."""
    try:
        return _parse(item, us_only)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        return None


def _parse(item: dict, us_only: bool) -> Listing | None:
    """The body of _listing; may raise on rows of an unexpected shape."""
    d = _dict(item.get("MatchedObjectDescriptor"))
    details = _dict(_dict(d.get("UserArea")).get("Details"))
    countries = {
        str(x.get("CountryCode") or "").strip() for x in _list(d.get("PositionLocation")) if isinstance(x, dict)
    }
    countries.discard("")
    if us_only and countries and not any(c.lower() in US for c in countries):
        return None
    who = str(_dict(details.get("WhoMayApply")).get("Name") or "")
    reqs = " ".join([who, _text(details.get("KeyRequirements")), _text(details.get("Requirements"))]).lower()
    remote = details.get("RemoteIndicator")
    hints = {
        "salary": _salary(d),
        "employment_type": ", ".join(
            str(x.get("Name") or "") for x in _list(d.get("PositionSchedule")) if isinstance(x, dict)
        ),
        "category": ", ".join(str(x.get("Name") or "") for x in _list(d.get("JobCategory")) if isinstance(x, dict)),
        "country": ", ".join(sorted(countries)),
    }
    if _CITIZEN.search(reqs):
        hints["citizenship_required"] = "yes"
    if remote is True or str(remote).lower() == "true":
        hints["remote"] = "yes"
    elif str(details.get("TeleworkEligible")).lower() == "true":
        hints["remote"] = "hybrid"
    company = str(d.get("OrganizationName") or d.get("DepartmentName") or "")
    return Listing(
        company=company,
        title=str(d.get("PositionTitle") or ""),
        url=str(d.get("PositionURI") or ""),
        location=str(d.get("PositionLocationDisplay") or "")[:500],
        posted_at=_date(d.get("PublicationStartDate")),
        description=_description(d, details),
        ats="usajobs",
        ats_job_id=str(item.get("MatchedObjectId") or "")[:100] or None,
        source_ref=str(d.get("PositionID") or "")[:300] or None,
        hints=hints,
    )


def _search(query: str, opts: FeedOptions, headers: dict, limit: int) -> list[Listing]:
    """One keyword search, page by page, until the results run out or `limit` rows are read."""
    params = {
        "Keyword": query,
        "ResultsPerPage": min(PAGE_SIZE, max(1, limit)),
        "Fields": "Full",
        "SortField": "opendate",
        "SortDirection": "desc",
    }
    if opts.since:
        days = (date.today() - opts.since).days
        if days <= MAX_DAYS:  # older than 60 days: every open job is newer anyway
            params["DatePosted"] = max(0, days)
    out: list[Listing] = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        data = get_json(URL, params={**params, "Page": page}, headers=headers)
        result = _dict(_dict(data).get("SearchResult"))
        items = _list(result.get("SearchResultItems"))
        if not items:
            break
        out += [x for x in (_listing(i, opts.us_only) for i in items if isinstance(i, dict)) if x is not None]
        try:
            pages = int(_dict(result.get("UserArea")).get("NumberOfPages") or 0)
        except (TypeError, ValueError):
            pages = 0
        if page >= pages or len(items) < params["ResultsPerPage"] or len(out) >= limit:
            break
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """Open federal jobs matching each of opts.queries (newest first, deduped by USAJOBS control number,
    MatchedObjectId)."""
    headers = _headers()
    seen: set[str] = set()
    out: list[Listing] = []
    for i, query in enumerate(opts.queries):
        if len(out) >= opts.max_results:
            break
        if i:
            time.sleep(PAUSE)
        for x in _search(query, opts, headers, opts.max_results - len(out)):
            k = x.ats_job_id or x.url
            if k not in seen:
                seen.add(k)
                out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="usajobs",
        description="USAJOBS (OPM): US federal civilian jobs via the official search API; mostly U.S. citizens only",
        fetch=fetch,
        key_env="USAJOBS_API_KEY",
        extra_env=("USAJOBS_EMAIL",),
        terms="Free API key from developer.usajobs.gov; User-Agent must be the registering email. Public data, reuse "
        "allowed; don't bypass rate limits, imply OPM endorsement, or misstate data. Apply via the official "
        "announcement. 500 rows/page, 10,000 rows/query.",
        default=False,
    )
)
