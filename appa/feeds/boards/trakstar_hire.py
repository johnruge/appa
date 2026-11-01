"""Trakstar Hire (formerly Recruiterbox) job boards, through each company's public jobs RSS feed (no key).

GET https://{slug}.hire.trakstar.com/jobfeeds/{slug} is the "Jobs RSS Feed" linked from every careers page.
One response lists every open posting (it matches the board's sitemap; the HTML careers page shows only 25)
with the title, the posting link (/jobs/{fkId}), the published date, the full description HTML, city/state/
country, position type and team; the channel title is "Jobs at {Company}". robots.txt only disallows
/prepare* and /published_jobs_list_v2*. The JSON API (/api/v1/openings/) needs a login, so it isn't used.
Old {slug}.recruiterbox.com links redirect to the Trakstar board, so they're recognised too.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under hire.trakstar.com
_JOB_PATH = re.compile(r"^/jobs/(fk[A-Za-z0-9]{1,40})/?$")
_DOMAINS = (".hire.trakstar.com", ".recruiterbox.com")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "jsapi",
    "help",
    "support",
    "blog",
    "status",
    "docs",
    "cdn",
    "static",
    "assets",
    "mail",
    "auth",
    "login",
    "hire",
    "careers",
    "jobs",
}
_JOB = "{https://recruiterbox.com/rss/job/}"
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Trakstar Hire subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad trakstar hire slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The jobs RSS feed URL for a board."""
    s = quote(_check(slug), safe="")
    return f"https://{s}.hire.trakstar.com/jobfeeds/{s}"


def _slug_of_host(host: str) -> str | None:
    """The board label of a {slug}.hire.trakstar.com or {slug}.recruiterbox.com host, or None."""
    host = host.lower().rstrip(".")
    for d in _DOMAINS:
        if host.endswith(d):
            label = host[: -len(d)]
            if "." in label or label in _NOT_BOARDS:
                return None
            try:
                return _check(label)
            except ValueError:
                return None
    return None


def board_of(url: str) -> str | None:
    """The board slug of a Trakstar Hire (or legacy Recruiterbox) link, or None."""
    try:
        return _slug_of_host(urlparse(url.strip()).hostname or "")
    except (AttributeError, ValueError):
        return None


def _day(v: str | None) -> date | None:
    """'Fri, 16 Oct 2026 00:00:00 +0530' -> date(2026, 10, 16) (the date as stated), or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _text(item, tag: str) -> str:
    """An element's text, whitespace-collapsed ("" when missing)."""
    return " ".join((item.findtext(tag) or "").split())


def _listing(item, slug: str, company: str) -> Listing:
    """One feed item as a Listing (ValueError when the link isn't a posting on this board or a field is bad)."""
    link = (item.findtext("link") or "").strip()
    p = urlparse(link)
    m = _JOB_PATH.fullmatch(p.path or "")
    if not m or _slug_of_host(p.hostname or "") != slug:
        raise ValueError(f"not a posting on {slug}: {link[:200]!r}")
    fk = m.group(1)
    places = [_text(item, _JOB + k) for k in ("locationCity", "locationState", "locationCountry")]
    text, hidden = html_to_text(item.findtext("description") or "")
    hints = {
        "country": places[2],
        "employment_type": _text(item, _JOB + "positionType").lower(),
        "category": _text(item, _JOB + "team"),
    }
    return Listing(
        company=company,
        title=_text(item, "title"),
        url=f"https://{quote(slug, safe='')}.hire.trakstar.com/jobs/{quote(fk, safe='')}",
        location=", ".join(x for x in places if x),
        posted_at=_day(item.findtext("pubDate")),
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats="trakstar_hire",
        ats_job_id=fk,
        source_ref=f"{slug}:{fk}",
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's Trakstar Hire board, with descriptions (one request)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    channel = ET.fromstring(get_bytes(feed_url(slug))).find("channel")
    if channel is None:
        return []
    company = re.sub(r"^\s*Jobs at\s+", "", channel.findtext("title") or "").strip() or slug
    out, seen = [], set()
    for item in channel.findall("item"):
        try:
            x = _listing(item, slug, company)
        except ValueError as e:
            log.debug("skipping trakstar hire item on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


BOARD = register_board(
    BoardFeed(
        ats="trakstar_hire",
        fetch=fetch,
        board_of=board_of,
        example="zeitview",
        terms="Public per-company jobs RSS feed linked from each careers page (no key; robots.txt allows /jobfeeds); "
        "one request per board.",
        hosts=("hire.trakstar.com",),
    )
)
