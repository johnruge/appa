"""HiBob (Bob) careers sites ({slug}.careers.hibob.com), through the JSON API the careers page itself uses.

GET https://{slug}.careers.hibob.com/api/job-ad with the header `companyidentifier: {slug}` (the careers SPA
sends it; without it the API answers 401) returns the whole board in one response, no paging:
{"filterGroups": {...}, "jobAdDetails": [...]}. Each job ad has an id (uuid), title, department, employmentType,
site, country, workspaceType (Remote/Hybrid/On site), publishedAt, optional pay-transparency fields, and its
text as HTML in description/responsibilities/requirements/benefits. GET /api/career-site (same header) gives
the board's companyName. The human posting is https://{slug}.careers.hibob.com/jobs/{id}. No key; the careers
host has no robots.txt (every path serves the SPA).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

DOMAIN = "careers.hibob.com"
MAX_DESC = 100_000
# A subdomain label: letters, digits and inner hyphens (dots or underscores can't be one DNS label here).
_SLUG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NOT_BOARDS = {"www", "app", "api", "dev", "stage", "sandbox"}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "on_site": "no", "on site": "no", "on-site": "no", "onsite": "no"}
_TYPES = {
    "full time": "full_time",
    "full-time": "full_time",
    "fulltime": "full_time",
    "part time": "part_time",
    "part-time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "intern": "internship",
    "internship": "internship",
    "temporary": "temporary",
    "temp": "temporary",
}
_SECTIONS = (
    ("description", ""),
    ("responsibilities", "Responsibilities"),
    ("requirements", "Requirements"),
    ("benefits", "Benefits"),
)


def _check(slug: str) -> str:
    """The slug, if it is a safe HiBob careers subdomain; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() in _NOT_BOARDS:
        raise ValueError(f"bad hibob slug: {slug!r}")
    return slug.lower()


def base_url(slug: str) -> str:
    """The board's careers origin, e.g. https://acme.careers.hibob.com."""
    return f"https://{quote(_check(slug), safe='')}.{DOMAIN}"


def _headers(slug: str) -> dict:
    """The header the careers API needs to know which company is asked for."""
    return {"companyidentifier": _check(slug), "Accept": "application/json"}


def _day(v) -> date | None:
    """'2026-09-22T15:25:24.114077945Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _company(slug: str) -> str:
    """The board's company name from /api/career-site, else a name made from the slug."""
    try:
        d = get_json(f"{base_url(slug)}/api/career-site", headers=_headers(slug))
    except Exception as e:  # the board is still useful without the real name
        log.debug("hibob company name for %s failed: %s", slug, e)
        d = None
    name = str(d.get("companyName") or "").strip() if isinstance(d, dict) else ""
    return name or slug.replace("-", " ").title()


def _text(j: dict) -> str:
    """The posting's sections as plain text, labelled, with hidden text flagged."""
    labels = j.get("sectionLabels") if isinstance(j.get("sectionLabels"), dict) else {}
    parts = []
    for key, default in _SECTIONS:
        raw = j.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text, hidden = html_to_text(raw)
        text = mark_hidden(text, hidden)
        head = str(labels.get(key) or default).strip()
        parts.append(f"{head}\n{text}" if head and text else text)
    return "\n\n".join(p for p in parts if p)[:MAX_DESC]


def _salary(j: dict) -> str:
    """'90000-120000 USD/Yearly' from the pay-transparency fields, or ''."""
    lo, hi = j.get("payTransparencyMinSalary"), j.get("payTransparencyMaxSalary")
    if lo in (None, "") and hi in (None, ""):
        return ""
    rng = "-".join(f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in (lo, hi) if v not in (None, ""))
    cur, per = j.get("payTransparencySalaryCurrency") or "", j.get("payTransparencySalaryPayPeriod") or ""
    return f"{rng} {cur}".strip() + (f"/{per}" if per else "")


def _location(j: dict) -> str:
    """'Atlanta, United States' from the site and country (one of them when they repeat)."""
    site, country = str(j.get("site") or "").strip(), str(j.get("country") or "").strip()
    if not site or country.lower() in site.lower():
        return site or country
    return f"{site}, {country}" if country else site


def _listing(j: dict, slug: str, company: str) -> Listing:
    """One job ad as a Listing (ValueError when a required field is missing or bad)."""
    job_id = str(j.get("id") or "")
    if not _ID.fullmatch(job_id):
        raise ValueError(f"bad hibob job id: {job_id!r}")
    kind = str(j.get("workspaceTypeId") or j.get("workspaceType") or "").strip().lower()
    etype = str(j.get("employmentType") or "").strip()
    hints = {
        "remote": _REMOTE.get(kind, ""),
        "country": str(j.get("country") or ""),
        "category": str(j.get("department") or ""),
        "salary": _salary(j),
        "employment_type": _TYPES.get(etype.lower(), etype),
    }
    return Listing(
        company=company,
        title=str(j.get("title") or ""),
        url=f"{base_url(slug)}/jobs/{quote(job_id, safe='')}",
        location=_location(j)[:500],
        posted_at=_day(j.get("publishedAt")),
        description=_text(j),
        ats="hibob",
        ats_job_id=job_id,
        source_ref=job_id,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's HiBob careers site, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    data = get_json(f"{base_url(slug)}/api/job-ad", headers=_headers(slug))
    rows = (data.get("jobAdDetails") if isinstance(data, dict) else None) or []
    if not isinstance(rows, list) or not rows:
        return []
    company, out, seen = _company(slug), [], set()
    for j in rows:
        try:
            x = _listing(j, slug, company)
        except (ValueError, TypeError, AttributeError) as e:
            log.debug("skipping hibob job on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The slug of a {slug}.careers.hibob.com[/jobs/{id}] link, or None."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if not host.endswith("." + DOMAIN):
        return None
    label = host[: -len(DOMAIN) - 1]
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="hibob",
        fetch=fetch,
        board_of=board_of,
        example="aerocloudsystems",
        terms="Public careers-site JSON API used by {slug}.careers.hibob.com pages (no key; needs the "
        "companyidentifier header the page sends). Two requests per board: job ads (with text) and company name.",
        hosts=(DOMAIN,),
    )
)
