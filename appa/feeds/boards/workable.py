"""Workable job boards (apply.workable.com/<slug>/), through Workable's public careers-widget API.

`GET https://apply.workable.com/api/v1/widget/accounts/<slug>?details=true` returns the account's name and
every published job in one response, each with its full HTML description (requirements and benefits
included), so a board is one request and needs no describe() step. Unknown accounts return 404. The
widget API is the one Workable offers for embedding a company's openings on its own site; it is
rate-limited by IP, so the BoardFeed lists its host for pacing. The posting page is
https://apply.workable.com/<slug>/j/<shortcode>/ and the shortcode is the posting's stable ID.

The per-job endpoint `GET /api/v2/accounts/<slug>/jobs/<shortcode>` backs describe(), for listings that
reach appa without a description.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources import http
from appa.sources.text import html_to_text, mark_hidden

API = "https://apply.workable.com/api/v1/widget/accounts/"
DETAIL = "https://apply.workable.com/api/v2/accounts/"
SITE = "https://apply.workable.com/"
MAX_DESC = 100_000

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHORTCODE = re.compile(r"^[A-Za-z0-9]{1,40}$")
# apply.workable.com/<first segment> values that are not company accounts.
_NOT_ACCOUNTS = {"j", "api", "static", "assets", "en", "login", "signup"}
# <x>.workable.com subdomains that belong to Workable itself, not a company board.
_NOT_SUBDOMAINS = {
    "www",
    "apply",
    "jobs",
    "api",
    "app",
    "help",
    "resources",
    "blog",
    "engineering",
    "status",
    "developers",
    "workable",
    "static",
    "assets",
    "careers-page",
    "id",
    "auth",
    "login",
}
_LEVELS = {
    "internship": "intern",
    "entry level": "entry",
    "associate": "entry",
    "mid-senior level": "mid",
    "director": "senior",
    "executive": "senior",
}


def _slug(slug: str) -> str:
    """The slug, if it is a plausible Workable account name; ValueError otherwise."""
    if not isinstance(slug, str) or not _SLUG.match(slug):
        raise ValueError(f"not a Workable account slug: {slug!r}"[:200])
    return slug


def _text(raw: str) -> str:
    """Posting HTML as text, with hidden text flagged, capped."""
    text, hidden = html_to_text(raw or "")
    return mark_hidden(text, hidden)[:MAX_DESC]


def _location(job: dict) -> str:
    """'City, Region, Country' for each listed location, joined with '; '."""
    locs = [x for x in job.get("locations") or [] if isinstance(x, dict) and not x.get("hidden")]
    if not locs:
        locs = [{"city": job.get("city"), "region": job.get("state"), "country": job.get("country")}]
    parts = [", ".join(str(p) for p in (x.get("city"), x.get("region"), x.get("country")) if p) for x in locs]
    return "; ".join(p for p in parts if p)


def _hints(job: dict) -> dict[str, str]:
    """Facts the widget states: employment type, level, remote, country, category, degree."""
    countries = {
        str(x.get("countryCode") or x.get("country"))
        for x in job.get("locations") or []
        if isinstance(x, dict) and (x.get("countryCode") or x.get("country"))
    }
    etype = str(job.get("employment_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "employment_type": etype,
        "level": _LEVELS.get(str(job.get("experience") or "").strip().lower(), ""),
        "remote": "yes" if job.get("telecommuting") is True else "",
        "country": ", ".join(sorted(countries)) or str(job.get("country") or ""),
        "category": str(job.get("function") or job.get("department") or ""),
        "degrees": str(job.get("education") or ""),
    }


def _posted(job: dict) -> date | None:
    """The publish date, if the widget gives a valid one."""
    try:
        return date.fromisoformat(str(job.get("published_on") or job.get("created_at") or "")[:10])
    except ValueError:
        return None


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published posting on this Workable account, with descriptions (one request)."""
    opts = opts or FeedOptions()
    slug = _slug(slug)
    data = http.get_json(API + quote(slug, safe=""), params={"details": "true"})
    company = str(data.get("name") or slug).strip() or slug
    out: list[Listing] = []
    for job in data.get("jobs") or []:
        if len(out) >= opts.max_results:
            break
        if not isinstance(job, dict):
            continue
        code = str(job.get("shortcode") or "")
        posted = _posted(job)
        if not _SHORTCODE.match(code) or (opts.since and posted and posted < opts.since):
            continue
        try:
            out.append(
                Listing(
                    company=company,
                    title=str(job.get("title") or ""),
                    url=f"{SITE}{quote(slug, safe='')}/j/{code}/",
                    location=_location(job),
                    posted_at=posted,
                    description=_text(str(job.get("description") or "")),
                    ats="workable",
                    ats_job_id=code,
                    source_ref=f"{slug}:{code}",
                    hints=_hints(job),
                )
            )
        except ValueError:
            continue
    return out


def describe(listing: Listing) -> str:
    """Full posting text (description, requirements, benefits) from the per-job endpoint."""
    if listing.ats_job_id is None:
        return ""
    slug = _slug(board_of(listing.url) or "")
    code = listing.ats_job_id
    if not _SHORTCODE.match(code):
        raise ValueError(f"not a Workable shortcode: {code!r}"[:200])
    job = http.get_json(f"{DETAIL}{quote(slug, safe='')}/jobs/{quote(code, safe='')}")
    raw = "".join(str(job.get(k) or "") for k in ("description", "requirements", "benefits"))
    return _text(raw)


def board_of(url: str) -> str | None:
    """The Workable account slug in a link (apply.workable.com/<slug>/... or <slug>.workable.com), else None."""
    try:
        p = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if host == "apply.workable.com":
        first = next((s for s in p.path.split("/") if s), "")
        slug = first.lower()
    elif host.endswith(".workable.com") and host.count(".") == 2:
        slug = host.removesuffix(".workable.com")
        if slug in _NOT_SUBDOMAINS:
            return None
    else:
        return None
    if not slug or slug in _NOT_ACCOUNTS or not _SLUG.match(slug):
        return None
    return slug


BOARD = register_board(
    BoardFeed(
        ats="workable",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="tickpick",
        terms="Workable's public careers-widget API (no key); rate-limited by IP, so poll boards politely.",
        hosts=("apply.workable.com",),
    )
)
