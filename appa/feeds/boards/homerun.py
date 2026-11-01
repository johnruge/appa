"""Homerun career sites (mostly Dutch/EU small companies), through each company's public Atom job feed (no key).

GET https://feed.homerun.co/{feed_slug} returns one static Atom document with every published job: the
feed title and each entry's author/name are the company, link[rel=alternate] is the human posting page
(https://{slug}.homerun.co/{job-slug}, or the company's own career domain), id is the stable job ID
(job_xxx), description is the full posting HTML, plus department/name, location/name, type/name
("Full-time", "Full-time / Part-time"...), salary_indication ("N/A" when unset) and updated
("YYYY-MM-DD HH:MM:SS"). There is no paging: the whole board is one file.

<updated> is the posting's creation date, not a freshness signal: still-open roles (Moneybird's 2026
internship) carry 2021 dates. Listings therefore get posted_at=None and opts.since is not applied, so
long-running open roles survive appa's age filters; the feed only lists published jobs anyway.

Each career page https://{slug}.homerun.co/ advertises its feed with <link rel="alternate"
type="application/atom+xml" href="https://feed.homerun.co/{feed_slug}">. The feed slug has matched the
subdomain on every board checked (jobs, moneybird), so fetch() only requests the feed (one request).
robots.txt on the career sites is empty apart from the sitemap.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

A = "{http://www.w3.org/2005/Atom}"
FEED_HOST = "feed.homerun.co"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label / one path segment
_FEED_PATH = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9-]{0,62})(?:_atom_[0-9a-f]{16,128}\.xml)?/?$")
_NOT_BOARDS = {
    "www",
    "feed",
    "feeds",
    "app",
    "api",
    "cdn",
    "404",
    "help",
    "support",
    "docs",
    "blog",
    "status",
    "auth",
    "login",
    "id",
    "academy",
    "developers",
    "developer",
    "assets",
    "static",
    "media",
    "mail",
    "go",
    "info",
    "partners",
    "partner",
    "careers-api",
}
_TYPES = {
    "full-time": "full_time",
    "part-time": "part_time",
    "internship": "internship",
    "freelance": "contract",
    "contract": "contract",
    "temporary": "temporary",
    "traineeship": "internship",
}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, lowercased, if it is a safe Homerun subdomain / feed name; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad homerun slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The Atom feed URL for a feed slug."""
    return f"https://{FEED_HOST}/{quote(_check(slug), safe='')}"


def _name(entry, tag: str) -> str:
    """The text of <tag><name>...</name></tag> in an entry."""
    return (entry.findtext(f"{A}{tag}/{A}name") or "").strip()


def _employment(raw: str) -> str:
    """'Full-time / Part-time' -> 'full_time, part_time' (unknown types kept, lowercased)."""
    parts = [p.strip().lower() for p in raw.split("/") if p.strip()]
    return ", ".join(dict.fromkeys(_TYPES.get(p, p.replace(" ", "_")) for p in parts))


def _link(entry) -> str:
    """The entry's human posting URL (link rel=alternate, else the first link)."""
    links = entry.findall(f"{A}link")
    alt = [x for x in links if x.get("rel", "alternate") == "alternate"] or links
    return (alt[0].get("href") or "").strip() if alt else ""


def _description(entry) -> str:
    """The posting HTML (description, else content) as text, hidden text flagged."""
    raw = entry.findtext(f"{A}description") or entry.findtext(f"{A}content") or ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(entry, company: str) -> Listing:
    """One Atom entry as a Listing (ValueError when a required field is missing or bad)."""
    jid = (entry.findtext(f"{A}id") or "").strip()[:100] or None
    salary = (entry.findtext(f"{A}salary_indication") or "").strip()
    hints = {
        "category": _name(entry, "department"),
        "employment_type": _employment(_name(entry, "type")),
        "salary": "" if salary.upper() in ("", "N/A") else salary,
    }
    return Listing(
        company=_name(entry, "author") or company,
        title=(entry.findtext(f"{A}title") or "").strip(),
        url=_link(entry),
        location=_name(entry, "location"),
        posted_at=None,
        description=_description(entry),
        ats="homerun",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published job on this Homerun career site, with descriptions (one request, no paging; opts.since
    ignored)."""
    opts = opts or FeedOptions()
    root = ET.fromstring(get_bytes(feed_url(slug)))
    company = (root.findtext(f"{A}title") or "").strip() or slug
    out, seen = [], set()
    for entry in root.findall(f"{A}entry"):
        try:
            x = _listing(entry, company)
        except ValueError as e:
            log.debug("skipping homerun entry on %s: %s", slug, e)
            continue
        if (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The slug of a {slug}.homerun.co or feed.homerun.co/{slug} link, or None (custom career domains aren't
    recognised)."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    if host == FEED_HOST:
        m = _FEED_PATH.fullmatch(p.path)
        label = m.group(1) if m else ""
    elif host.endswith(".homerun.co"):
        label = host[: -len(".homerun.co")]
        if "." in label or label in _NOT_BOARDS:
            return None
    else:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="homerun",
        fetch=fetch,
        board_of=board_of,
        example="moneybird",
        terms="Public Atom job feed (no key) that each Homerun career page advertises via <link rel=alternate>; "
        "one static file per company, no paging, one request per board; career-site robots.txt is empty apart from "
        "the sitemap.",
        hosts=(FEED_HOST,),
    )
)
