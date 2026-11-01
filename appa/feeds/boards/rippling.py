"""Rippling ATS job boards (ats.rippling.com/{slug}/jobs), through the JSON API the board page itself uses.

GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=N&pageSize=100 returns one page of the board as
{"items": [...], "page", "pageSize", "totalItems", "totalPages"}. Each item is one (job, location) pair, so a
job open in several places appears several times with the same id; fetch() merges them into one Listing.
Items carry the title, department, locations (country, workplaceType) and the human posting URL, but no
description, date or company name. GET https://ats.rippling.com/api/v2/board/{slug}/jobs/{id} returns one
posting with its description HTML, companyName, createdOn and pay range: fetch() asks it once per board for
the company name, and describe() uses it for the posting text. No key; robots.txt only disallows /internal/.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

HOST = "ats.rippling.com"
API = f"https://{HOST}/api/v2/board"
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_NOT_BOARDS = {"api", "internal", "_next", "static", "assets", "jobs", "embed", "careers"}
_REMOTE = {"REMOTE": "yes", "HYBRID": "hybrid", "ON_SITE": "no"}
_TYPES = {
    "SALARIED_FT": "full_time",
    "HOURLY_FT": "full_time",
    "SALARIED_PT": "part_time",
    "HOURLY_PT": "part_time",
    "INTERN": "internship",
    "CONTRACTOR": "contract",
    "TEMP": "temporary",
}


def _check(slug: str) -> str:
    """The slug, if it is a safe Rippling board slug; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad rippling slug: {slug!r}")
    return slug


def jobs_url(slug: str) -> str:
    """The board's paged jobs API URL."""
    return f"{API}/{quote(_check(slug), safe='')}/jobs"


def job_url(slug: str, job_id: str) -> str:
    """One posting's detail API URL."""
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise ValueError(f"bad rippling job id: {job_id!r}")
    return f"{jobs_url(slug)}/{quote(job_id, safe='')}"


def _day(v) -> date | None:
    """'2026-03-31T17:37:26-07:00' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _company(slug: str, job_id: str) -> str:
    """The board's company name from one posting's detail, else a name made from the slug."""
    fallback = re.sub(r"[-_]+(careers|jobs|open-positions)$", "", slug).replace("-", " ").replace("_", " ").title()
    try:
        d = get_json(job_url(slug, job_id))
    except Exception as e:  # the list is still useful without the real name
        log.debug("rippling company name for %s failed: %s", slug, e)
        return fallback
    name = str(d.get("companyName") or "").strip() if isinstance(d, dict) else ""
    return name or fallback


def _merge(rows: list[dict]) -> dict[str, dict]:
    """Rows grouped by job id, in board order, each with all its locations."""
    jobs: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        j = jobs.setdefault(str(r["id"]), {**r, "locations": []})
        j["locations"] += [loc for loc in r.get("locations") or [] if isinstance(loc, dict)]
    return jobs


def _listing(j: dict, company: str) -> Listing:
    """One merged job as a Listing (ValueError when a required field is missing or bad)."""
    url = str(j.get("url") or "")
    if (urlparse(url).hostname or "").lower() != HOST:
        raise ValueError("posting url is not on ats.rippling.com")
    locs = j["locations"]
    names = list(dict.fromkeys(str(loc.get("name") or "").strip() for loc in locs if loc.get("name")))
    countries = list(dict.fromkeys(str(loc["country"]) for loc in locs if loc.get("country")))
    kinds = {str(loc.get("workplaceType") or "") for loc in locs}
    dept = str((j.get("department") or {}).get("name") or "").strip()
    hints = {
        "remote": _REMOTE.get(kinds.pop(), "") if len(kinds) == 1 else "",
        "country": ", ".join(countries),
        "category": "" if dept == "-" else dept,
    }
    return Listing(
        company=company,
        title=str(j.get("name") or ""),
        url=url,
        location="; ".join(names)[:500],
        ats="rippling",
        ats_job_id=str(j["id"]),
        source_ref=str(j["id"]),
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's Rippling board (no descriptions: see describe())."""
    opts = opts or FeedOptions()
    url, rows = jobs_url(slug), []
    for page in range(MAX_PAGES):
        if page:
            time.sleep(0.5)
        data = get_json(url, params={"page": page, "pageSize": PAGE_SIZE})
        items = (data.get("items") if isinstance(data, dict) else None) or []
        rows += items
        if not items or page + 1 >= int(data.get("totalPages") or 0) or len(_merge(rows)) > opts.max_results:
            break
    jobs = _merge(rows)
    company = _company(slug, next(iter(jobs))) if jobs else slug
    out = []
    for j in jobs.values():
        try:
            out.append(_listing(j, company))
        except (ValueError, TypeError, AttributeError) as e:
            log.debug("skipping rippling job on %s: %s", slug, e)
            continue
        if len(out) >= opts.max_results:
            break
    return out


def _pay(ranges) -> str:
    """'Pay range (US): 185000-240000 USD/YEAR' lines from the detail's payRangeDetails."""
    lines = []
    for p in ranges if isinstance(ranges, list) else []:
        if isinstance(p, dict) and (p.get("rangeStart") or p.get("rangeEnd")):
            rng = "-".join(
                f"{p[k]:.0f}" if isinstance(p.get(k), (int, float)) else str(p[k])
                for k in ("rangeStart", "rangeEnd")
                if p.get(k)
            )
            lines.append(
                f"Pay range ({p.get('location') or 'all'}): {rng} {p.get('currency') or ''}/{p.get('frequency') or ''}"
            )
    return "\n".join(lines)


def describe(listing: Listing) -> str:
    """The posting's full text (company blurb, role, pay range) from its detail endpoint, hidden text flagged."""
    parts = [p for p in urlparse(listing.url).path.split("/") if p]
    if parts and _LOCALE.fullmatch(parts[0]) and len(parts) == 4:
        parts = parts[1:]
    if (urlparse(listing.url).hostname or "").lower() != HOST or len(parts) != 3 or parts[1] != "jobs":
        raise ValueError("not a rippling posting url")
    d = get_json(job_url(parts[0], listing.ats_job_id or parts[2]))
    desc = d.get("description") if isinstance(d, dict) else None
    raw = "\n".join(str(desc.get(k) or "") for k in ("company", "role")) if isinstance(desc, dict) else str(desc or "")
    text, hidden = html_to_text(raw)
    return "\n\n".join(x for x in (mark_hidden(text, hidden), _pay(d.get("payRangeDetails"))) if x)[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The board slug of an ats.rippling.com/[locale/]{slug}/jobs[/...] link, or None."""
    try:
        u = urlparse(url.strip())
        host = (u.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if host != HOST:
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 3 and _LOCALE.fullmatch(parts[0]) and parts[2] == "jobs":
        parts = parts[1:]
    if len(parts) < 2 or parts[1] != "jobs" or parts[0].lower() in _NOT_BOARDS:
        return None
    try:
        return _check(parts[0])
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="rippling",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="boom-supersonic",
        terms="Public board JSON API used by ats.rippling.com pages (no key); paged 100 per request, 0.5s apart, "
        "plus one detail request per board for the company name.",
        hosts=(HOST,),
    )
)
