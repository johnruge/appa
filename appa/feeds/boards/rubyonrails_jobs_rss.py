"""The official Ruby on Rails job board (jobs.rubyonrails.org), through its public RSS feed (no key).

GET https://jobs.rubyonrails.org/jobs.rss returns every open posting (about a dozen; it takes no paging
parameters and posts arrive a few a week, so polling daily is enough). Each item has a title
('{Title} at {Company}'), description (the full job description as escaped HTML), pubDate, and
link/guid (https://jobs.rubyonrails.org/jobs/{id}-{slug}). The number in the link is the stable job ID.
There are no structured location/salary/type fields in the feed, so the only hint is the links found in
the description (they help discover company boards).

It's one site, not an ATS, so it has a single board slug, "rubyonrails"; board_of() maps
jobs.rubyonrails.org job and feed links to it. robots.txt allows everything; the site publishes no
terms page.
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

HOST = "jobs.rubyonrails.org"
FEED = f"https://{HOST}/jobs.rss"
SLUG = "rubyonrails"
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_JOB_PATH = re.compile(r"^/jobs/(\d{1,20})(?:-[A-Za-z0-9-]{0,200})?/?$")
_HREF = re.compile(r"""href\s*=\s*["'](https?://[^"'\s<>]{1,500})["']""", re.I)


def _check(slug: str) -> str:
    """The slug, if it is this site's one board; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() != SLUG:
        raise ValueError(f"bad rubyonrails slug: {slug!r}")
    return SLUG


def _host(url: str) -> str:
    """The lowercased host of a link, or "" when it can't be parsed."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return ""


def _day(v: str | None) -> date | None:
    """'Thu, 15 Oct 2026 11:58:25 +0000' -> date, or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _text(it, tag: str) -> str:
    """An element's stripped text, or ""."""
    return (it.findtext(tag) or "").strip()


def _description(raw: str) -> str:
    """The item's HTML body as text, hidden text flagged."""
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _links(raw: str) -> str:
    """The http(s) links in the description HTML, deduped, space separated (at most 10)."""
    out: list[str] = []
    for u in _HREF.findall(raw or ""):
        if u not in out:
            out.append(u)
    return " ".join(out[:10])


def _split_title(full: str) -> tuple[str, str]:
    """'{Title} at {Company}' -> (title, company); ValueError when there's no ' at '."""
    title, sep, company = full.rpartition(" at ")
    if not sep or not title.strip() or not company.strip():
        raise ValueError(f"title has no company: {full[:100]!r}")
    return title.strip(), company.strip()


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when the link isn't a job link or a field is missing)."""
    link = _text(it, "link") or _text(it, "guid")
    m = _JOB_PATH.match(urlparse(link).path) if _host(link) == HOST else None
    if not m:
        raise ValueError(f"not a rubyonrails job link: {link[:100]!r}")
    title, company = _split_title(_text(it, "title"))
    raw = _text(it, "description")
    return Listing(
        company=company,
        title=title,
        url=f"https://{HOST}{urlparse(link).path}",
        posted_at=_day(it.findtext("pubDate")),
        description=_description(raw),
        ats="rubyonrails",
        ats_job_id=m.group(1),
        source_ref=m.group(1),
        hints={"links": _links(raw)},
    )


def fetch(slug: str = SLUG, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open Rails job board posting (one request; the feed isn't paged)."""
    _check(slug)
    opts = opts or FeedOptions()
    root = ET.fromstring(get_bytes(FEED))
    channel = root.find("channel")
    out, seen = [], set()
    for it in channel.findall("item") if channel is not None else []:
        try:
            x = _listing(it)
        except ValueError as e:
            log.debug("skipping rubyonrails item: %s", e)
            continue
        if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """ "rubyonrails" for a jobs.rubyonrails.org job or feed link, else None."""
    if not isinstance(url, str) or _host(url) != HOST:
        return None
    path = urlparse(url.strip()).path
    return SLUG if _JOB_PATH.match(path) or path.rstrip("/") == "/jobs.rss" else None


BOARD = register_board(
    BoardFeed(
        ats="rubyonrails",
        fetch=fetch,
        board_of=board_of,
        example=SLUG,
        terms="Public RSS feed of the official Rails job board (no key, no paging, ~a dozen open posts; poll "
        "daily); robots.txt allows all; no published terms page.",
        hosts=(HOST,),
    )
)
