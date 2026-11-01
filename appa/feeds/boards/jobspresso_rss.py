"""Jobspresso (jobspresso.co), a curated remote-jobs board, through its public WP Job Manager RSS feed (no key).

GET https://jobspresso.co/feed/job_feed/ lists the 10 newest postings. Each item has the title (HTML entities), the
human posting page (https://jobspresso.co/job/{slug}/), pubDate, a guid carrying the WordPress post ID
(...?post_type=job_listing&p={id}, the stable ID), the full HTML description (content:encoded) and
job_listing:* fields, HTML-escaped inside CDATA ('AI &amp; Data', decoded here): company, location (the remote
region, e.g. 'United States, Canada' or 'Worldwide'), salary, and two swapped labels: job_type holds the
category ('Engineer') and job_category the employment type ('Full Time').
dc:creator repeats 'Company<br>⚲&nbsp;Location' and is the fallback.

Every posting is remote, so the board is site-wide: "all" is the only slug, and job/listing links on
jobspresso.co (/job/, /remote-work/, /browse-jobs/, /feed/job_feed/) map to it.
The real apply link (the company's ATS) is only on the posting page; appa's scout finds it there.

Terms: the site grants personal, non-commercial viewing (no scraping clause). robots.txt disallows every query
URL ('Disallow: /*?'), so paging (?paged=N) and keyword search (?search_keywords=) are off limits: fetch makes
exactly one request, to the query-free feed path. The site posts ~35 jobs a day and the channel updates hourly,
so polling every 1-2 hours sees every posting.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED = "https://jobspresso.co/feed/job_feed/"
JL = "{https://jobspresso.co}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
ALL = "all"
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ID = re.compile(r"^\d{1,20}$")
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_BOARD_PATHS = ("/job/", "/remote-work/", "/browse-jobs/", "/feed/job_feed/")
_TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "freelance": "contract",
    "contract": "contract",
    "internship": "internship",
    "temporary": "temporary",
}


def _check(slug: str) -> str:
    """The slug, if it is "all" (the only board; search URLs are robots-disallowed); else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() != ALL:
        raise ValueError(f"bad jobspresso slug: {slug!r}")
    return ALL


def _is_jobspresso(url: str) -> bool:
    """True when the link is on jobspresso.co itself."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return False
    return host == "jobspresso.co" or host.endswith(".jobspresso.co")


def _day(v: str | None) -> date | None:
    """'Fri, 30 Oct 2026 11:21:44 +0000' -> date, or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _text(it, tag: str) -> str:
    """A child element's stripped text ('' when missing)."""
    return (it.findtext(tag) or "").strip()


def _plain(it, tag: str) -> str:
    """A child element's text with its HTML entities decoded ('AI &amp; Data' -> 'AI & Data')."""
    return html.unescape(_text(it, tag)).strip()


def _post_id(guid: str) -> str | None:
    """The WordPress post ID from a guid like https://jobspresso.co/?post_type=job_listing&p=164692."""
    if not _is_jobspresso(guid):
        return None
    p = (parse_qs(urlparse(guid).query).get("p") or [""])[0]
    return p if _ID.fullmatch(p) else None


def _creator(raw: str) -> tuple[str, str]:
    """(company, location) from dc:creator 'Company<br>⚲&nbsp;Location'."""
    company, _, loc = raw.partition("<br>")
    loc = html.unescape(loc).replace("\xa0", " ").replace("⚲", "")
    return html.unescape(company).strip(), " ".join(loc.split())


def _links(raw_html: str) -> str:
    """Non-Jobspresso http(s) links in the description, space separated (for board discovery)."""
    out: list[str] = []
    for h in _HREF.findall(raw_html):
        h = html.unescape(h).strip()
        if h.lower().startswith(("http://", "https://")) and not _is_jobspresso(h) and h not in out:
            out.append(h)
    return " ".join(out[:10])


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when a required field is missing or bad)."""
    link = _text(it, "link")
    if not _is_jobspresso(link):
        raise ValueError(f"not a jobspresso link: {link[:100]!r}")
    by_company, by_loc = _creator(_text(it, f"{DC}creator"))
    company = _plain(it, f"{JL}company") or by_company
    region = " ".join(_plain(it, f"{JL}location").split()) or by_loc
    raw = it.findtext(CONTENT) or it.findtext("description") or ""
    text, hidden = html_to_text(raw)
    jid = _post_id(_text(it, "guid"))
    hints = {
        "remote": "yes",
        "country": region,
        "salary": _plain(it, f"{JL}salary"),
        "category": _plain(it, f"{JL}job_type"),
        "links": _links(raw),
        "employment_type": _TYPES.get(_plain(it, f"{JL}job_category").lower(), ""),
    }
    return Listing(
        company=company,
        title=_plain(it, "title"),
        url=link,
        location=f"Remote ({region})" if region else "Remote",
        posted_at=_day(it.findtext("pubDate")),
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats="jobspresso",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def _items() -> list:
    """The items on the query-free feed page (the 10 newest postings)."""
    root = ET.fromstring(get_bytes(FEED))
    channel = root.find("channel")
    return channel.findall("item") if channel is not None else []


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """The 10 newest postings ("all" only), with descriptions, one request; drops ones older than opts.since."""
    opts = opts or FeedOptions()
    _check(slug)
    out, seen = [], set()
    for it in _items():
        try:
            x = _listing(it)
        except ValueError as e:
            log.debug("skipping jobspresso item: %s", e)
            continue
        if (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """ "all" for a jobspresso.co job or listing link (the site is one board), else None."""
    if not isinstance(url, str) or not _is_jobspresso(url):
        return None
    path = (urlparse(url.strip()).path or "/").lower()
    return ALL if path.startswith(_BOARD_PATHS) or path == "/feed/job_feed" else None


BOARD = register_board(
    BoardFeed(
        ats="jobspresso_rss",
        fetch=fetch,
        board_of=board_of,
        example=ALL,
        terms="Public WP Job Manager RSS feed (no key); site terms allow personal, non-commercial viewing. robots.txt "
        "disallows query URLs, so only the query-free feed page (10 newest) is read: one request per run; poll "
        "every 1-2 hours to see every posting.",
        hosts=("jobspresso.co",),
    )
)
