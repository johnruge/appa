"""Manatal career pages, through the public Career Page API (no key).

GET https://api.manatal.com/open/v3/career-page/{slug}/jobs/?page=N&page_size=100 returns a DRF-style page
({count, next, previous, results[]}) of a company's published jobs, each with the description HTML,
location parts, a remote flag and the contract type. There is no posting date. The company's display
name comes from https://api.manatal.com/open/v3/career-page/{slug}/ (each job's organization_name is a
department or, for agencies, a client). The human posting is https://www.careers-page.com/{slug}/job/{hash}.
Manatal documents this Career Page API separately from its token-only Open API (developers.manatal.com).
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote, urlparse

import httpx

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://api.manatal.com/open/v3/career-page/"
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_HASH = re.compile(r"^[A-Za-z0-9]{1,40}$")
_NOT_BOARDS = {"static", "media", "api", "job", "jobs", "assets", "admin", "login", "robots.txt", "favicon.ico"}
_API_HOSTS = ("api.manatal.com", "core.api.manatal.com")
_PAGE_HOSTS = ("careers-page.com", "www.careers-page.com")


def _check(slug: str) -> str:
    """The slug, if it is a safe career-page slug; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad manatal slug: {slug!r}")
    return slug


def api_url(slug: str) -> str:
    """The Career Page API jobs URL for a board."""
    return f"{API}{quote(_check(slug), safe='')}/jobs/"


def _company(slug: str) -> str:
    """The career page's company name, or the slug when the settings call fails or has no name."""
    try:
        data = get_json(f"{API}{quote(_check(slug), safe='')}/")
    except (httpx.HTTPError, ValueError) as e:
        log.debug("manatal settings for %s unavailable: %s", slug, e)
        return slug
    name = data.get("name") if isinstance(data, dict) else None
    return str(name).strip() if name and str(name).strip() else slug


def _hints(j: dict) -> dict[str, str]:
    """The facts the job states, under HINT_KEYS names."""
    remote = {True: "yes", False: "no"}.get(j.get("is_remote"), "")
    return {
        "remote": remote,
        "country": str(j.get("country") or ""),
        "employment_type": str(j.get("contract_details") or ""),
        "category": str(j.get("organization_name") or ""),
    }


def _description(raw) -> str:
    """Description HTML as text, hidden text flagged."""
    text, hidden = html_to_text(str(raw or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(j: dict, slug: str, company: str) -> Listing:
    """One job as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict):
        raise ValueError("not a job")
    jid, h = str(j.get("id") or ""), str(j.get("hash") or "")
    if not jid.isdigit() or not _HASH.fullmatch(h):
        raise ValueError("job without a valid id/hash")
    location = str(j.get("location_display") or "")
    if j.get("is_remote") is True and "remote" not in location.lower():
        location = f"Remote ({location})" if location else "Remote"
    return Listing(
        company=company,
        title=str(j.get("position_name") or ""),
        url=f"https://www.careers-page.com/{quote(slug, safe='')}/job/{quote(h, safe='')}",
        location=location[:500],
        description=_description(j.get("description")),
        ats="manatal",
        ats_job_id=jid,
        source_ref=h,
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published job on this company's Manatal career page, with descriptions (paged, capped)."""
    opts = opts or FeedOptions()
    url = api_url(slug)
    company = _company(slug)
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(0.5)
        data = get_json(url, params={"page": page, "page_size": PAGE_SIZE})
        rows = (data.get("results") if isinstance(data, dict) else None) or []
        for j in rows:
            try:
                x = _listing(j, slug, company)
            except ValueError as e:
                log.debug("skipping manatal job on %s: %s", slug, e)
                continue
            if x.ats_job_id in seen:
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if not rows or not data.get("next"):
            break
    return out


def board_of(url: str) -> str | None:
    """The slug of a careers-page.com/{slug} or api.manatal.com/open/v3/career-page/{slug} link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    parts = [x for x in p.path.split("/") if x]
    if host in _PAGE_HOSTS and parts:
        slug = parts[0]
    elif host in _API_HOSTS and len(parts) >= 4 and parts[:3] == ["open", "v3", "career-page"]:
        slug = parts[3]
    else:
        return None
    if slug.lower() in _NOT_BOARDS:
        return None
    try:
        return _check(slug)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="manatal",
        fetch=fetch,
        board_of=board_of,
        example="manatal",
        terms="Public Career Page API (no key) that Manatal's hosted career pages use; one settings call plus "
        "one call per 100 jobs, 0.5s apart.",
        hosts=("api.manatal.com",),
    )
)
