"""NoDesk (nodesk.co), a curated remote-jobs board, through the RSS feeds the site publishes.

GET https://nodesk.co/remote-jobs/index.xml (linked from every page as rel=alternate) returns the 10 newest
postings; each role category has its own feed at /remote-jobs/{category}/index.xml (engineering, design,
product...). The "slug" of this board is the category, or "all" for the main feed. Each <item> has the title
as '{Title} at {Company}', the NoDesk job page as link/guid, an RFC 822 pubDate and a plain-text excerpt of
the posting (region restrictions are usually in it). There is no paging, so appa should poll every few hours.

The feed is Hugo output that uses HTML entities XML doesn't define (&rsquo;), so they are turned into
numeric references before defusedxml parses it. NoDesk's terms forbid scraping or crawling its pages, so
only the feed is read: job pages are never fetched (no describe(), no apply-link resolution); the job
page's link slug is the posting's identity.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from html.entities import name2codepoint
from urllib.parse import quote, urlparse

from defusedxml import ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

BASE = "https://nodesk.co/remote-jobs/"
CATEGORIES = {
    "all": "",
    "engineering": "Engineering",
    "design": "Design",
    "product": "Product",
    "marketing": "Marketing",
    "sales": "Sales",
    "customer-support": "Customer Support",
    "operations": "Operations",
}
HOSTS = ("nodesk.co", "www.nodesk.co")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_JOB_PATH = re.compile(r"^/remote-jobs/([a-z0-9][a-z0-9-]{0,99})/?$")
_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug (a category name or 'all'), if it is safe; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad nodesk slug: {slug!r}")
    return slug


def feed_url(slug: str) -> str:
    """The RSS URL for a category ('all' is the main feed)."""
    slug = _check(slug)
    return f"{BASE}index.xml" if slug == "all" else f"{BASE}{quote(slug, safe='')}/index.xml"


def _entity(m: re.Match) -> str:
    """An HTML-only entity as a numeric reference XML understands; unknown names are escaped."""
    name = m.group(1)
    if name in _XML_ENTITIES:
        return m.group(0)
    cp = name2codepoint.get(name)
    return f"&#{cp};" if cp else f"&amp;{name};"


def parse(raw: bytes) -> list:
    """The <item> elements of a NoDesk feed."""
    text = _ENTITY.sub(_entity, raw.decode("utf-8", errors="replace"))
    return ET.fromstring(text.encode("utf-8")).findall("./channel/item")


def _day(v: str) -> date | None:
    """'Thu, 29 Oct 2026 08:00:00 +0200' -> date (as NoDesk dates it), or None."""
    try:
        return parsedate_to_datetime(v).date()
    except (TypeError, ValueError, IndexError):
        return None


def _job_slug(link: str) -> str:
    """The job page's slug from a nodesk.co/remote-jobs/{slug}/ link; else ValueError."""
    p = urlparse(link)
    m = _JOB_PATH.fullmatch(p.path)
    if p.scheme != "https" or (p.hostname or "").lower() not in HOSTS or not m or m.group(1) in CATEGORIES:
        raise ValueError(f"not a nodesk job link: {link[:100]!r}")
    return m.group(1)


def _location(text: str) -> str:
    """The 'Location: ...' line some excerpts carry, or ''."""
    m = re.search(r"^\s*Location\s*:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip()[:300] if m else ""


def _listing(item, slug: str) -> Listing:
    """One <item> as a Listing (ValueError when a required field is missing or bad)."""

    def get(tag: str) -> str:
        """The element's text, stripped."""
        return (item.findtext(tag) or "").strip()

    link = get("link") or get("guid")
    jid = _job_slug(link)
    full = html.unescape(get("title"))  # titles are double-escaped ('&amp;amp;')
    title, sep, company = full.rpartition(" at ")
    if not sep:
        raise ValueError(f"title without company: {full[:100]!r}")
    text, hidden = html_to_text(get("description"))
    desc = mark_hidden(text, hidden)[:MAX_DESC]
    hints = {"remote": "yes", "category": CATEGORIES.get(slug, slug.replace("-", " ").title())}
    return Listing(
        company=company,
        title=title,
        url=f"https://nodesk.co/remote-jobs/{jid}/",
        location=_location(desc) or "Remote",
        posted_at=_day(get("pubDate")),
        description=desc,
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """The newest postings in this NoDesk feed (one request; the feed has no pages)."""
    opts = opts or FeedOptions()
    items = parse(get_bytes(feed_url(slug)))
    out, seen = [], set()
    for item in items:
        try:
            x = _listing(item, slug)
        except ValueError as e:
            log.debug("skipping nodesk item in %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """'all' or the category for a nodesk.co/remote-jobs/ (category) page or feed link; None otherwise."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host not in HOSTS:
        return None
    parts = [s for s in p.path.split("/") if s]
    if parts and parts[-1] == "index.xml":
        parts = parts[:-1]
    if parts == ["remote-jobs"]:
        return "all"
    if len(parts) == 2 and parts[0] == "remote-jobs" and parts[1] in CATEGORIES and parts[1] != "all":
        return parts[1]
    return None


BOARD = register_board(
    BoardFeed(
        ats="nodesk",
        fetch=fetch,
        board_of=board_of,
        example="engineering",
        terms="Public RSS feeds the site links (no key); only the 10 newest items per feed, poll every ~6 h. "
        "NoDesk's terms forbid scraping/crawling its pages, so job pages are never fetched.",
        hosts=("nodesk.co",),
    )
)
