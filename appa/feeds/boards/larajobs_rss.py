"""LaraJobs (larajobs.com), a PHP/Laravel job board, through its public RSS feed (no key).

GET https://larajobs.com/feed returns the newest postings (about 5; it takes no paging parameters and
posts arrive a few a week, so polling daily is enough). Each item has title, link
(https://larajobs.com/job/{id}, which redirects to the employer's apply page), pubDate, dc:creator, and
fields in the https://larajobs.com namespace: company, location ('Remote/USA'), job_type ('FULL_TIME'),
salary ('$160,000.00 - $180,000.00') and tags (comma list). The number in the link is the stable job ID.
The description elements are present but empty, and the job page is only a redirect, so there's no text.

It's one site, not an ATS, so it has a single board slug, "larajobs"; board_of() maps larajobs.com job
and feed links to it. robots.txt allows everything; the site publishes no terms page.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED = "https://larajobs.com/feed"
SLUG = "larajobs"
JOB = "{https://larajobs.com}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_JOB_ID = re.compile(r"^/job/(\d{1,20})/?$")
_LEVELS = {
    "senior": "senior",
    "junior": "entry",
    "entry level": "entry",
    "mid-level": "mid",
    "mid level": "mid",
    "intern": "intern",
    "internship": "intern",
}


def _check(slug: str) -> str:
    """The slug, if it is this site's one board; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() != SLUG:
        raise ValueError(f"bad larajobs slug: {slug!r}")
    return SLUG


def _host(url: str) -> str:
    """The lowercased host of a link, or "" when it can't be parsed."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return ""


def _day(v: str | None) -> date | None:
    """'Sun, 25 Oct 2026 18:21:28 +0000' -> date, or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _text(it, tag: str) -> str:
    """An element's stripped text, or ""."""
    return (it.findtext(tag) or "").strip()


def _description(it) -> str:
    """The item's HTML body as text, hidden text flagged ("" when the feed leaves it empty, as it usually does)."""
    raw = _text(it, f"{CONTENT}encoded") or _text(it, "description")
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _hints(it, location: str) -> dict[str, str]:
    """Facts the item states: job type, salary, remote/hybrid in the location, a level tag, the tags."""
    tags = [t.strip() for t in _text(it, f"{JOB}tags").split(",") if t.strip()]
    loc = location.lower()
    remote = "hybrid" if "hybrid" in loc else "yes" if "remote" in loc else ""
    level = next((_LEVELS[t.lower()] for t in tags if t.lower() in _LEVELS), "")
    return {
        "employment_type": _text(it, f"{JOB}job_type").lower(),
        "salary": _text(it, f"{JOB}salary"),
        "remote": remote,
        "level": level,
        "category": ", ".join(tags),
    }


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when the link isn't a larajobs job link or a field is missing)."""
    link = _text(it, "link")
    m = _JOB_ID.match(urlparse(link).path) if _host(link) == "larajobs.com" else None
    if not m:
        raise ValueError(f"not a larajobs job link: {link[:100]!r}")
    location = _text(it, f"{JOB}location")
    return Listing(
        company=_text(it, f"{JOB}company") or _text(it, f"{DC}creator"),
        title=_text(it, "title"),
        url=f"https://larajobs.com/job/{m.group(1)}",
        location=location,
        posted_at=_day(it.findtext("pubDate")),
        description=_description(it),
        ats="larajobs",
        ats_job_id=m.group(1),
        source_ref=_text(it, "guid")[:300] or m.group(1),
        hints=_hints(it, location),
    )


def fetch(slug: str = SLUG, opts: FeedOptions | None = None) -> list[Listing]:
    """The newest LaraJobs postings (one request; the feed isn't paged)."""
    _check(slug)
    opts = opts or FeedOptions()
    root = ET.fromstring(get_bytes(FEED))
    channel = root.find("channel")
    out, seen = [], set()
    for it in channel.findall("item") if channel is not None else []:
        try:
            x = _listing(it)
        except ValueError as e:
            log.debug("skipping larajobs item: %s", e)
            continue
        if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """ "larajobs" for a larajobs.com job or feed link, else None."""
    host = _host(url) if isinstance(url, str) else ""
    if host not in ("larajobs.com", "www.larajobs.com"):
        return None
    path = urlparse(url.strip()).path
    return SLUG if _JOB_ID.match(path) or path.rstrip("/") == "/feed" else None


BOARD = register_board(
    BoardFeed(
        ats="larajobs",
        fetch=fetch,
        board_of=board_of,
        example=SLUG,
        terms="Public RSS feed (no key, no paging, ~5 newest posts; poll daily); robots.txt allows all; "
        "no published terms page. Job links redirect to the employer's apply page.",
        hosts=("larajobs.com",),
    )
)
