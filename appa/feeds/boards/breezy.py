"""Breezy HR job boards, through each company's public careers-site JSON feed (no key).

GET https://{slug}.breezy.hr/json returns every published position on a company's board in one response:
id, title, the human posting link, published date, employment type, locations (with remote/hybrid
details), department, the posted salary text and the company name. It has no description, so describe()
reads the posting page once and takes the schema.org JobPosting description Breezy embeds for Google Jobs.
The board's robots.txt allows both paths; the feed is what Breezy's own careers widget reads.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under breezy.hr
_POSTING = re.compile(r"^/p/([A-Za-z0-9][A-Za-z0-9-]{0,199})(?:/apply)?/?$")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "help",
    "support",
    "blog",
    "status",
    "developer",
    "developers",
    "docs",
    "marketplace",
    "partners",
    "cdn",
    "static",
    "assets",
    "mail",
    "auth",
    "login",
    "id",
    "attachments",
    "gallery-cdn",
    "careers",
    "jobs",
}
_TYPES = {
    "fullTime": "full_time",
    "partTime": "part_time",
    "contract": "contract",
    "temporary": "temporary",
    "internship": "internship",
    "seasonal": "temporary",
    "perDiem": "part_time",
    "volunteer": "volunteer",
}
_REMOTE = {"remote": "yes", "remote-location": "yes", "hybrid": "hybrid"}
_RANK = ["", "no", "hybrid", "yes"]  # the most remote location wins
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Breezy subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad breezy slug: {slug!r}")
    return slug.lower()


def api_url(slug: str) -> str:
    """The careers-site JSON feed URL for a board."""
    return f"https://{quote(_check(slug), safe='')}.breezy.hr/json"


def _day(v) -> date | None:
    """'2026-10-21T19:50:14.960Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _location(p: dict) -> tuple[str, list[str], str]:
    """(location text, country names, remote hint) from the position's locations."""
    locs = [x for x in (p.get("locations") or [p.get("location")]) if isinstance(x, dict)]
    names, countries, remote = [], [], ""
    for loc in locs:
        if loc.get("name") and loc["name"] not in names:
            names.append(str(loc["name"]))
        c = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else None
        if c and c not in countries:
            countries.append(str(c))
        kind = loc["remote_details"].get("value") if isinstance(loc.get("remote_details"), dict) else None
        r = _REMOTE.get(str(kind)) or ("yes" if loc.get("is_remote") else "no" if loc.get("is_remote") is False else "")
        remote = max(remote, r, key=_RANK.index)
    text = "; ".join(names)
    if remote == "yes" and "remote" not in text.lower():
        text = f"Remote ({text})" if text else "Remote"
    return text[:500], countries, remote


def _listing(p: dict, slug: str) -> Listing:
    """One position as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(p, dict):
        raise ValueError("not a position")
    location, countries, remote = _location(p)
    pid = str(p.get("id") or "") or None
    company = p.get("company") if isinstance(p.get("company"), dict) else {}
    etype = (p.get("type") or {}).get("id") if isinstance(p.get("type"), dict) else ""
    hints = {
        "employment_type": _TYPES.get(str(etype or ""), str(etype or "")),
        "remote": remote,
        "country": ", ".join(countries),
        "salary": str(p.get("salary") or ""),
        "category": str(p.get("department") or ""),
    }
    return Listing(
        company=str(company.get("name") or slug),
        title=str(p.get("name") or ""),
        url=str(p.get("url") or ""),
        location=location,
        posted_at=_day(p.get("published_date")),
        ats="breezy",
        ats_job_id=pid,
        source_ref=str(p.get("friendly_id") or pid or "")[:300] or None,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published position on this company's Breezy board, without descriptions (one request)."""
    opts = opts or FeedOptions()
    data = get_json(api_url(slug))
    out, seen = [], set()
    for p in data if isinstance(data, list) else []:
        try:
            x = _listing(p, slug)
        except ValueError as e:
            log.debug("skipping breezy position on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def _slug_of_host(host: str) -> str | None:
    """The board label of a {slug}.breezy.hr host, or None."""
    host = host.lower().rstrip(".")
    if not host.endswith(".breezy.hr"):
        return None
    label = host[: -len(".breezy.hr")]
    if "." in label or label in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.breezy.hr link, or None (custom career domains aren't recognised)."""
    try:
        return _slug_of_host(urlparse(url.strip()).hostname or "")
    except (AttributeError, ValueError):
        return None


def posting_url(url: str) -> str:
    """The canonical posting page for a Breezy posting link, rebuilt from validated parts; else ValueError."""
    p = urlparse(url.strip())
    slug = _slug_of_host(p.hostname or "")
    m = _POSTING.fullmatch(p.path or "")
    if not slug or not m or p.scheme not in ("http", "https"):
        raise ValueError(f"not a breezy posting link: {url[:200]!r}")
    return f"https://{quote(slug, safe='')}.breezy.hr/p/{quote(m.group(1), safe='')}"


def _job_posting(page: str) -> dict | None:
    """The schema.org JobPosting object embedded in a posting page, if any."""
    for tag in BeautifulSoup(page, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return None


def describe(listing: Listing) -> str:
    """The posting's full text from its page's JobPosting data, hidden text flagged ("" when absent)."""
    page, _ = get_text(posting_url(listing.url))
    jp = _job_posting(page)
    if not jp or not isinstance(jp.get("description"), str):
        return ""
    text, hidden = html_to_text(jp["description"])
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="breezy",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="vagaro",
        terms="Public careers-site JSON feed and posting pages (no key; robots.txt allows them); one request per "
        "board, one per described posting.",
        hosts=("breezy.hr",),
    )
)
