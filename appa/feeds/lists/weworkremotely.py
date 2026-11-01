"""We Work Remotely: a large remote-only job board with public per-category RSS feeds
(https://weworkremotely.com/remote-job-rss-feed). Each feed returns every open posting in that category
(no pagination, ttl 60 minutes) with the full HTML description. appa reads the software categories
(programming, back-end, front-end, full-stack, DevOps) and dedupes across them by link, since a posting
can appear in more than one feed. Titles are "Company: Role". The site-wide remote-jobs.rss is skipped
because it caps each category at 10 postings. robots.txt allows everything outside account/admin pages.

Terms are mixed: the RSS page (/remote-job-rss-feed) lets anyone use the feeds as long as links are attributed
back to WWR, but the API terms (/api-terms-and-guidelines) forbid scraping, copying or storing WWR data and
building a competing job service. So this feed is off by default (opt in by name) and every Listing keeps the
WWR posting link. Posting pages block non-browser clients (403 to curl), so appa relies on the RSS description
and must not re-fetch the page over HTTP; the links still open in a real browser.
"""

from __future__ import annotations

import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import defusedxml.ElementTree as ET

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

BASE = "https://weworkremotely.com/categories/"
CATEGORIES = (
    "remote-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-front-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-devops-sysadmin-jobs",
)
FEED_SLEEP = 1.0
MAX_DESC = 100_000
_TYPES = {"full-time": "full_time", "part-time": "part_time", "contract": "contract", "internship": "internship"}
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]")
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_MAX_HINT = 300
# Country names that contain " and ", kept whole when splitting a list like "Ireland and Mexico".
_AND_NAMES = (
    "Antigua and Barbuda",
    "Bosnia and Herzegovina",
    "Heard Island and McDonald Islands",
    "Saint Kitts and Nevis",
    "Saint Pierre and Miquelon",
    "Saint Vincent and the Grenadines",
    "Sao Tome and Principe",
    "South Georgia and the South Sandwich Islands",
    "Svalbard and Jan Mayen",
    "Trinidad and Tobago",
    "Turks and Caicos Islands",
    "United Kingdom of Great Britain and Northern Ireland",
    "Wallis and Futuna",
)
_SLUG = re.compile(r"^/remote-jobs/([A-Za-z0-9][A-Za-z0-9-]{0,99})/?$")


def _text(item, tag: str) -> str:
    """The stripped text of a child element ('' when missing)."""
    el = item.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _date(s: str) -> date | None:
    """An RFC 822 RSS date as a date (None if missing or invalid)."""
    try:
        return parsedate_to_datetime(s).date() if s else None
    except (TypeError, ValueError, IndexError):
        return None


def _is_wwr(url: str) -> bool:
    """True when the link is on weworkremotely.com itself."""
    host = (urlparse(url).hostname or "").lower()
    return host == "weworkremotely.com" or host.endswith(".weworkremotely.com")


def _slug(url: str) -> str | None:
    """The posting's slug from its weworkremotely.com/remote-jobs/<slug> link (its stable ID), or None."""
    if not _is_wwr(url):
        return None
    m = _SLUG.match(urlparse(url).path)
    return m.group(1) if m else None


def _countries(raw: str) -> str:
    """The country field without flag emoji, comma separated, e.g. 'United States of America, Mexico'."""
    s = " ".join(_FLAG.sub("", raw).split())
    for i, name in enumerate(_AND_NAMES):
        s = s.replace(name, f"\x00{i}\x00")
    s = s.replace(", and ", ", ").replace(" and ", ", ")
    for i, name in enumerate(_AND_NAMES):
        s = s.replace(f"\x00{i}\x00", name)
    return s.strip(", ")


def _links(raw_html: str) -> str:
    """Non-WWR http(s) links in the description, space separated, never cutting a URL at the hint cap."""
    out: list[str] = []
    for h in _HREF.findall(raw_html):
        h = "".join(h.split())
        if not h.lower().startswith(("http://", "https://")) or _is_wwr(h) or h in out:
            continue
        if len(" ".join(out + [h])) > _MAX_HINT:
            break
        out.append(h)
        if len(out) == 10:
            break
    return " ".join(out)


def _listing(item) -> Listing | None:
    """A Listing from one RSS <item>, or None for malformed items."""
    url = _text(item, "link") or _text(item, "guid")
    company, sep, title = _text(item, "title").partition(": ")
    if not (sep and _is_wwr(url)):
        return None
    raw = _text(item, "description")
    text, hidden = html_to_text(raw) if raw else ("", [])
    region, countries = _text(item, "region"), _countries(_text(item, "country"))
    if region == "USA Only" and not countries:
        countries = "United States"
    hints = {
        "remote": "yes",
        "category": _text(item, "category"),
        "employment_type": _TYPES.get(_text(item, "type").lower(), ""),
        "country": countries if len(countries) <= _MAX_HINT else countries[: _MAX_HINT + 1].rpartition(", ")[0],
        "links": _links(raw),
    }
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location="; ".join(x for x in (region, countries) if x)[:500],
            posted_at=_date(_text(item, "pubDate")),
            description=mark_hidden(text, hidden)[:MAX_DESC],
            ats_job_id=_slug(url),
            source_ref=_slug(url),
            hints=hints,
        )
    except ValueError:
        return None


def _expired(item, today: date) -> bool:
    """True when the item's expires_at date has passed."""
    end = _date(_text(item, "expires_at"))
    return end is not None and end < today


def _today() -> date:
    """Today's date (a function so tests can pin it)."""
    return date.today()


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every open posting in the software category feeds, deduped by link, newest first."""
    out: dict[str, Listing] = {}
    today = _today()
    for i, cat in enumerate(CATEGORIES):
        if i:
            time.sleep(FEED_SLEEP)
        root = ET.fromstring(get_bytes(f"{BASE}{cat}.rss"))
        for item in root.iter("item"):
            if _expired(item, today):
                continue
            x = _listing(item)
            if x is None or x.url in out or (opts.since and x.posted_at and x.posted_at < opts.since):
                continue
            out[x.url] = x
        if len(out) >= opts.max_results:
            break
    rows = sorted(out.values(), key=lambda x: x.posted_at or date.min, reverse=True)
    return rows[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="weworkremotely",
        description="We Work Remotely (RSS): remote programming, back-end, front-end, full-stack and DevOps roles",
        fetch=fetch,
        terms="RSS page (weworkremotely.com/remote-job-rss-feed): anyone may use the feeds with links attributed back "
        "to WWR. API terms (/api-terms-and-guidelines) forbid scraping/copying/storing WWR data and competing job "
        "services, which conflicts with keeping full descriptions in appa's DB. Personal, non-redistributed use "
        "only; keep the WWR posting link; read /terms-and-conditions before enabling. Posting pages 403 non-browser "
        "clients, so use the RSS description and don't re-fetch pages.",
        default=False,
    )
)
