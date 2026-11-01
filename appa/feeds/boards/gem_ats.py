"""Gem ATS job boards, through Gem's public Job Board API (no key).

GET https://api.gem.com/job_board/v0/{slug}/job_posts/ returns every live post on a company's board in
one JSON array (no pagination): title, HTML content, location, location type (remote/hybrid/in_office),
employment type, offices, departments and the human posting link on jobs.gem.com/{slug}/{id}. The slug is
the board's "vanity URL path", the first path segment of jobs.gem.com links. Gem documents the read
endpoints as public (api.gem.com/job_board/v0/reference). The API carries no company name, so the company
is the slug, tidied up.
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

API = "https://api.gem.com/job_board/v0/"
_SLUG = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9._-]{0,99}$")  # real slugs may start with "-" (e.g. "-joinforage")
_NOT_BOARDS = {
    "api",
    "assets",
    "static",
    "_next",
    "favicon.ico",
    "robots.txt",
    "sitemap.xml",
    "login",
    "auth",
    "reference",
    "openapi.json",
}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "in_office": "no"}
_TYPES = {"intern": "internship"}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Gem vanity path; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or set(slug) <= {"-", "."}:
        raise ValueError(f"bad gem slug: {slug!r}")
    return slug


def api_url(slug: str) -> str:
    """The Job Board API job_posts URL for a board."""
    return f"{API}{quote(_check(slug), safe='')}/job_posts/"


def _company(slug: str) -> str:
    """A readable company name from the slug ('-joinforage' -> 'Joinforage', 'acme-ai' -> 'Acme Ai')."""
    words = [w for w in re.split(r"[-_.]+", slug) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or slug


def _day(v) -> date | None:
    """'2026-04-01T20:00:28.526Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _name(obj) -> str:
    """obj['name'] as text when obj is a dict, else ''."""
    return str(obj.get("name") or "").strip() if isinstance(obj, dict) else ""


def _locations(p: dict) -> list[str]:
    """Distinct location names from the post's offices, falling back to its location field."""
    names = [_name(o.get("location")) or _name(o) for o in p.get("offices") or [] if isinstance(o, dict)]
    names = [n for n in dict.fromkeys(names) if n]
    return names or [n for n in [_name(p.get("location"))] if n]


def _hints(p: dict, locations: list[str]) -> dict[str, str]:
    """The facts the post states, under HINT_KEYS names."""
    etype = str(p.get("employment_type") or "")
    countries = dict.fromkeys(n.split(",")[-1].split(" - ")[0].strip() for n in locations)
    return {
        "remote": _REMOTE.get(str(p.get("location_type") or ""), ""),
        "employment_type": _TYPES.get(etype, etype),
        "level": "intern" if etype == "intern" else "",
        "country": ", ".join(c for c in countries if c),
        "category": ", ".join(n for n in map(_name, p.get("departments") or []) if n),
    }


def _description(p: dict) -> str:
    """The post's HTML content as text, hidden text flagged."""
    text, hidden = html_to_text(str(p.get("content") or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(p: dict, slug: str) -> Listing:
    """One job post as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(p, dict):
        raise ValueError("not a job post")
    pid = str(p.get("id") or "") or None
    url = str(p.get("absolute_url") or "")
    if board_of(url) != slug:
        raise ValueError("posting link is not on this jobs.gem.com board")
    locations = _locations(p)
    return Listing(
        company=_company(slug),
        title=str(p.get("title") or ""),
        url=url,
        location="; ".join(locations)[:500],
        posted_at=_day(p.get("first_published_at") or p.get("created_at")),
        description=_description(p),
        ats="gem_ats",
        ats_job_id=pid,
        source_ref=pid,
        hints=_hints(p, locations),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every live post on this company's Gem board, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    data = get_json(api_url(slug))
    out, seen = [], set()
    for p in data if isinstance(data, list) else []:
        try:
            x = _listing(p, slug)
        except ValueError as e:
            log.debug("skipping gem post on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a jobs.gem.com/{slug}[/...] (or api.gem.com/job_board/v0/{slug}/...) link, or None."""
    try:
        p = urlparse(str(url).strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    parts = [s for s in p.path.split("/") if s]
    if host == "jobs.gem.com" and parts:
        slug = parts[0]
    elif host == "api.gem.com" and len(parts) >= 3 and parts[:2] == ["job_board", "v0"]:
        slug = parts[2]
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
        ats="gem_ats",
        fetch=fetch,
        board_of=board_of,
        example="linktree",
        terms="Gem's public Job Board API (read endpoints need no key; default limit 20 req/s); one request per board.",
        hosts=("api.gem.com",),
    )
)
