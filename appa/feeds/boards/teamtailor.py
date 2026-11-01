"""Teamtailor career sites, through each site's public jobs RSS feed (no key).

GET https://{slug}.teamtailor.com/jobs.rss lists a company's published jobs with the full description
HTML, publish date, remote status, department and locations (tt:location: name, city, country). The
channel title is the company name; each item's link is the human posting page, which may be on the
company's own career domain (e.g. jobs.tibber.com/jobs/8632993-...). The number in that link is the
stable job ID. Teamtailor documents the feed for sending job data to other systems (support article
"RSS feed: how-to guide"): 100 jobs by default, paged with per_page and offset.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

TT = "{https://teamtailor.com/locations}"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under teamtailor.com
_JOB_ID = re.compile(r"/jobs/(\d{1,20})(?:-|/|$)")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "docs",
    "help",
    "support",
    "blog",
    "status",
    "partner",
    "partners",
    "marketplace",
    "cdn",
    "static",
    "assets",
    "images",
    "media",
    "mail",
    "auth",
    "login",
    "id",
    "developer",
    "developers",
    "go",
    "info",
}
_REMOTE = {"fully": "yes", "hybrid": "hybrid", "none": "no", "onsite": "no"}
PER_PAGE = 100
MAX_PAGES = 50
PAGE_SLEEP = 0.5
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Teamtailor subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad teamtailor slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The jobs RSS URL for a career site."""
    return f"https://{quote(_check(slug), safe='')}.teamtailor.com/jobs.rss"


def _day(v: str | None) -> date | None:
    """'Thu, 29 Oct 2026 13:25:57 +0100' -> date, or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _location(it) -> tuple[str, list[str]]:
    """(location text, country names) from the item's tt:location entries."""
    parts, countries = [], []
    for loc in it.iter(f"{TT}location"):
        city, country = (loc.findtext(f"{TT}city") or "").strip(), (loc.findtext(f"{TT}country") or "").strip()
        bits = [b for b in (city or (loc.findtext(f"{TT}name") or "").strip(), country) if b]
        if bits:
            parts.append(", ".join(dict.fromkeys(bits)))
        if country and country not in countries:
            countries.append(country)
    return "; ".join(dict.fromkeys(parts))[:500], countries


def _description(raw: str) -> str:
    """The item's HTML description as text, hidden text flagged."""
    text, hidden = html_to_text(raw or "")
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(it, company: str) -> Listing:
    """One RSS item as a Listing (ValueError when a required field is missing or bad)."""
    link = (it.findtext("link") or "").strip()
    m = _JOB_ID.search(urlparse(link).path)
    jid = m.group(1) if m else None
    location, countries = _location(it)
    remote = _REMOTE.get((it.findtext("remoteStatus") or "").strip().lower(), "")
    if remote == "yes" and "remote" not in location.lower():
        location = f"Remote ({location})" if location else "Remote"
    hints = {
        "remote": remote,
        "country": ", ".join(countries),
        "category": (it.findtext(f"{TT}department") or "").strip(),
    }
    return Listing(
        company=company,
        title=(it.findtext("title") or "").strip(),
        url=link,
        location=location,
        posted_at=_day(it.findtext("pubDate")),
        description=_description(it.findtext("description")),
        ats="teamtailor",
        ats_job_id=jid,
        source_ref=(it.findtext("guid") or "").strip()[:300] or jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published job on this Teamtailor career site, with descriptions (100 per page, offset paging)."""
    opts = opts or FeedOptions()
    url = feed_url(slug)
    out, seen = [], set()
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAGE_SLEEP)
        root = ET.fromstring(get_bytes(url, params={"per_page": PER_PAGE, "offset": page * PER_PAGE}))
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        company = (channel.findtext("title") or "").strip() if channel is not None else ""
        new = 0
        for it in items:
            try:
                x = _listing(it, company or slug)
            except ValueError as e:
                log.debug("skipping teamtailor item on %s: %s", slug, e)
                continue
            if (x.ats_job_id or x.url) in seen:
                continue
            seen.add(x.ats_job_id or x.url)
            new += 1
            if opts.since and x.posted_at and x.posted_at < opts.since:
                continue
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if len(items) < PER_PAGE or not new:  # last page, or the site ignored offset
            break
    return out


def board_of(url: str) -> str | None:
    """The career site slug of a {slug}.teamtailor.com link, or None (custom career domains aren't recognised)."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if not host.endswith(".teamtailor.com"):
        return None
    label = host[: -len(".teamtailor.com")]
    if "." in label or label in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="teamtailor",
        fetch=fetch,
        board_of=board_of,
        example="career",
        terms="Public jobs RSS feed (no key), documented by Teamtailor for sending job data to other systems; "
        "100 jobs per request, paged with per_page/offset; robots.txt allows /jobs.",
        hosts=("teamtailor.com",),
    )
)
