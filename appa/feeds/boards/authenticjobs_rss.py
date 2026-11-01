"""Authentic Jobs (authenticjobs.com), a US-centric developer/design job board, through its public job RSS feed.

The site runs WP Job Manager and links its feed from every page footer ("RSS Feed"):
GET https://authenticjobs.com/?feed=job_feed (no key). Do not use /feed/, which is the blog. Each page has 10
items, newest first, paged with &paged=N (an empty page past the end); the whole board is only ~50 postings.
Each item has the title, the human posting link (https://authenticjobs.com/job/{id}/{slug}/), pubDate, a guid
ending in ?post_type=job_listing&p={id} (the stable post ID), the full description HTML (content:encoded) and
job_listing:company / location / job_type / job_category fields. dc:creator is the poster's username, not the
company. &job_categories={term} narrows the feed to one category.

The board mixes in design, marketing and non-tech roles, so fetch() keeps only titles that look like
engineering/developer/data roles. It's one site, not an ATS: the slug is "all" (the whole feed) or a WP Job
Manager category term (e.g. "back-end-engineering"), and board_of() maps any authenticjobs.com posting link to
"all". robots.txt asks for Crawl-delay: 3 (and disallows query URLs for crawlers); appa reads the advertised
feed a handful of pages a day and waits 3 s between pages.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED_URL = "https://authenticjobs.com/"
HOSTS = ("authenticjobs.com", "www.authenticjobs.com")
JL = "{https://authenticjobs.com}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_JOB_PATH = re.compile(r"^/job/(\d{1,12})(?:/|$)")
_POST_ID = re.compile(r"^\d{1,12}$")
_TECH = re.compile(
    r"engineer|developer|software|programmer|\bswe\b|\bsde\b|full[- ]?stack|front[- ]?end|back[- ]?end|"
    r"devops|\bsre\b|reliability|\bdata\b|machine learning|\bml\b|\bai\b|\bios\b|android|mobile|"
    r"platform|infrastructure|security|technical|technology|web|coder|scientist|architect|"
    r"intern\b|new grad|graduate|entry[- ]level|junior",
    re.I,
)
_SALARY = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\s*(?:-|–|—|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?\s?[kK]?"
)  # a range only
_US = re.compile(r"\bUnited States\b|\bUSA\b|\bU\.S\.|\bUS\b")
PAGE_SIZE = 10
MAX_PAGES = 50
PAGE_SLEEP = 3.0  # robots.txt Crawl-delay
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is "all" or a safe category term; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad authenticjobs slug: {slug!r}")
    return slug


def _day(v: str | None) -> date | None:
    """'Wed, 14 Oct 2026 16:24:05 +0000' -> date, or None."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def _post_id(guid: str, link: str) -> str | None:
    """The WordPress post ID from the guid (?p=), else from the /job/{id}/ link."""
    p = (parse_qs(urlparse(guid).query).get("p") or [""])[0]
    if _POST_ID.fullmatch(p):
        return p
    m = _JOB_PATH.match(urlparse(link).path)
    return m.group(1) if m else None


def _remote(location: str) -> str:
    """'hybrid' or 'yes' when the location says so, else ''."""
    low = location.lower()
    return "hybrid" if "hybrid" in low else "yes" if "remote" in low else ""


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when a required field is missing or bad)."""
    link = (it.findtext("link") or "").strip()
    if (urlparse(link).hostname or "").lower() not in HOSTS:
        raise ValueError(f"not an authenticjobs link: {link[:100]!r}")
    jid = _post_id((it.findtext("guid") or "").strip(), link)
    text, hidden = html_to_text(it.findtext(CONTENT) or it.findtext("description") or "")
    description = mark_hidden(text, hidden)[:MAX_DESC]
    location = (it.findtext(f"{JL}location") or "").strip()
    job_type = (it.findtext(f"{JL}job_type") or "").strip()
    salary = _SALARY.search(text)
    hints = {
        "remote": _remote(location),
        "country": "United States" if _US.search(location) else "",
        "employment_type": re.sub(r"[\s-]+", "_", job_type.lower()),
        "category": (it.findtext(f"{JL}job_category") or "").strip(),
        "salary": salary.group(0) if salary else "",
    }
    return Listing(
        company=(it.findtext(f"{JL}company") or "").strip(),
        title=(it.findtext("title") or "").strip(),
        url=link,
        location=location,
        posted_at=_day(it.findtext("pubDate")),
        description=description,
        ats="authenticjobs_rss",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Engineering-looking postings on Authentic Jobs (newest first, 10 per page), with descriptions."""
    opts = opts or FeedOptions()
    params = {"feed": "job_feed"}
    if _check(slug) != "all":
        params["job_categories"] = slug
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAGE_SLEEP)
        root = ET.fromstring(get_bytes(FEED_URL, params=params | ({"paged": page} if page > 1 else {})))
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        new, older = 0, False
        for it in items:
            try:
                x = _listing(it)
            except ValueError as e:
                log.debug("skipping authenticjobs item: %s", e)
                continue
            if (x.ats_job_id or x.url) in seen:
                continue
            seen.add(x.ats_job_id or x.url)
            new += 1
            if opts.since and x.posted_at and x.posted_at < opts.since:
                older = True  # newest first: the rest are older too
                continue
            if not _TECH.search(x.title):
                continue
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if len(items) < PAGE_SIZE or not new or older:
            break
    return out


def board_of(url: str) -> str | None:
    """ "all" for an authenticjobs.com posting link (/job/{id}/...), else None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host not in HOSTS:
        return None
    return "all" if _JOB_PATH.match(p.path) else None


BOARD = register_board(
    BoardFeed(
        ats="authenticjobs_rss",
        fetch=fetch,
        board_of=board_of,
        example="all",
        terms="Public WP Job Manager RSS feed linked from the site footer (no key); 10 items per page via &paged=N. "
        "Terms page has no automation clause; robots.txt sets Crawl-delay: 3 and disallows query URLs for "
        "crawlers, so poll at most daily and wait 3 s between pages.",
        hosts=("authenticjobs.com",),
    )
)
