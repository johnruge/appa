"""VueJobs (vuejobs.com): a Vue/Nuxt-focused frontend and fullstack job board, read through its public RSS feed.

The site links "Subscribe to our RSS feed" to https://app.vuejobs.com/feed/posts (vuejobs.com/feed redirects
there). One GET returns every active posting (about 1,100, ~900 KB, no pagination, no auth). Each <item> has
the job title, link (https://vuejobs.com/jobs/{company-slug}-{title-slug}?utm_source=rssfeed), guid (the link
without utm), pubDate and a short HTML teaser that starts with "<p><strong>Employer:</strong> {Company}</p>
<p><strong>Location:</strong> {Remote | City, ST, CC | ...}</p>"; company and location are read from there.
Locations end in ISO country codes, so with opts.us_only postings that name only non-US countries are dropped
("Remote" and blank stay in). Titles are not filtered here: appa's shared filter does that.

VueJobs isn't an ATS, so this "board" has a single slug, "posts" (the whole feed); board_of() returns it for any
vuejobs.com job link, so a VueJobs link found anywhere adds the feed to the rotation. The posting's identity is
its guid slug. The teaser is cut at ~200 characters, so description stays "" and describe() reads the posting
page's embedded Nuxt payload (one request): full text, plus apply link, seniority, sponsorship and salary.

robots.txt disallows /feed on vuejobs.com and everything on app.vuejobs.com for crawlers; the feed is offered to
subscribers, so appa reads it like a feed reader would: one request per run, at most daily. Posting pages
(/jobs/...) are allowed. The terms-of-use page returned HTTP 500 when this was written.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED_URL = "https://app.vuejobs.com/feed/posts"
JOB_URL = "https://vuejobs.com/jobs/"
SLUG = "posts"  # the only board: the whole feed
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_POST = re.compile(r"^/jobs/([a-z0-9][a-z0-9-]{0,299})/?$")
_FIELD = r"<strong>\s*{}\s*:?\s*</strong>\s*:?\s*(.*?)</p>"  # filled with a field name
_TAGS = re.compile(r"<[^>]+>")
_CC = re.compile(r"^[A-Z]{2}$")
_NUXT = re.compile(r"<script[^>]*\bid=[\"']__NUXT_DATA__[\"'][^>]*>(.*?)</script>", re.S | re.I)
_WRAPPERS = ("ShallowReactive", "Reactive", "Ref", "ShallowRef")


def _check(slug: str) -> str:
    """The slug if it is this board's ("posts"); ValueError for anything else."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug != SLUG:
        raise ValueError(f"bad vuejobs slug: {slug!r}")
    return slug


def _is_vuejobs(host: str) -> bool:
    """True for vuejobs.com and its subdomains only."""
    return host == "vuejobs.com" or host.endswith(".vuejobs.com")


def _post_slug(url: str) -> str | None:
    """The posting slug of a https://vuejobs.com/jobs/<slug> link (query ignored), or None."""
    try:
        p = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or (p.hostname or "").lower().rstrip(".") not in (
        "vuejobs.com",
        "www.vuejobs.com",
    ):
        return None
    m = _POST.match(p.path)
    return m.group(1) if m else None


def _job_id(slug: str) -> str:
    """The slug as a stable ID of at most 100 chars (long slugs keep a prefix plus a hash of the whole)."""
    if len(slug) <= 100:
        return slug
    return f"{slug[:87]}-{hashlib.sha1(slug.encode()).hexdigest()[:12]}"


def _field(raw: str, name: str) -> str:
    """The text after '<strong>{name}:</strong>' in the teaser HTML ('' when missing)."""
    m = re.search(_FIELD.format(re.escape(name)), raw, re.I | re.S)
    return " ".join(html.unescape(_TAGS.sub(" ", m.group(1))).split()) if m else ""


def _countries(location: str) -> list[str]:
    """The ISO country codes ending each '|'-separated location ('Denver, CO, US' -> ['US'])."""
    out: list[str] = []
    for part in location.split("|"):
        cc = part.strip().rpartition(",")[2].strip()
        if _CC.fullmatch(cc) and cc not in out:
            out.append(cc)
    return out


def _date(s: str) -> date | None:
    """An RFC 822 pubDate as a date (None if missing or invalid)."""
    try:
        return parsedate_to_datetime(s).date() if s else None
    except (TypeError, ValueError, IndexError):
        return None


def _text(item, tag: str) -> str:
    """The stripped text of a child element ('' when missing)."""
    el = item.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _listing(item) -> Listing:
    """One RSS <item> as a Listing (ValueError when it is malformed)."""
    slug = _post_slug(_text(item, "guid")) or _post_slug(_text(item, "link"))
    if not slug:
        raise ValueError("no vuejobs.com/jobs link")
    raw = _text(item, "description")
    location = _field(raw, "Location")
    countries = _countries(location)
    hints = {"country": ", ".join(countries), "remote": "yes" if location.lower() == "remote" else ""}
    return Listing(
        company=_field(raw, "Employer"),
        title=html.unescape(_text(item, "title")),
        url=JOB_URL + slug,
        location=location.replace(" | ", "; ")[:500],
        posted_at=_date(_text(item, "pubDate")),
        ats="vuejobs",
        ats_job_id=_job_id(slug),
        source_ref=slug,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every active posting in the feed (one request), newest first, minus non-US ones when opts.us_only."""
    opts = opts or FeedOptions()
    _check(slug)
    root = ET.fromstring(get_bytes(FEED_URL))
    out: dict[str, Listing] = {}
    for item in root.iter("item"):
        try:
            x = _listing(item)
        except ValueError as e:
            log.debug("skipping vuejobs item: %s", e)
            continue
        if x.ats_job_id in out or (opts.since and x.posted_at and x.posted_at < opts.since):
            continue
        cc = x.hints.get("country", "")
        if opts.us_only and cc and "US" not in cc.split(", "):
            continue
        out[x.ats_job_id] = x
    rows = sorted(out.values(), key=lambda x: x.posted_at or date.min, reverse=True)
    return rows[: opts.max_results]


def _resolve(data: list, i, depth: int = 0):
    """Value at index i of a Nuxt (devalue) payload, with references followed (depth-limited)."""
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(data) or depth > 20:
        return None
    x = data[i]
    if isinstance(x, list):
        if len(x) == 2 and x[0] in _WRAPPERS:
            return _resolve(data, x[1], depth + 1)
        return [_resolve(data, j, depth + 1) for j in x[:50]]
    if isinstance(x, dict):
        return {str(k): _resolve(data, v, depth + 1) for k, v in list(x.items())[:50]}
    return x


def _post(page: str, slug: str) -> dict | None:
    """The posting record with this slug from the page's __NUXT_DATA__ payload, or None."""
    m = _NUXT.search(page)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    for x in data:
        if isinstance(x, dict) and "apply_url" in x and _resolve(data, x.get("slug")) == slug:
            return {
                k: _resolve(data, x[k])
                for k in (
                    "title",
                    "about_role",
                    "description",
                    "locations",
                    "remote_countries",
                    "work_type",
                    "work_place",
                    "seniority",
                    "seniority_years",
                    "education",
                    "visa_sponsorship",
                    "work_authorization",
                    "security_clearance",
                    "salary",
                    "apply_url",
                    "source_url",
                    "internship",
                )
                if k in x
            }
    return None


def _short(v) -> str:
    """A payload value as one short line ('' for empty)."""
    if v is None or v == [] or v == {}:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(s for s in (_short(x) for x in v) if s)
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if x not in (None, ""))
    return " ".join(str(v).split())[:300]


def _facts(p: dict) -> list[str]:
    """'Label: value' lines for the structured fields VueJobs shows on the posting."""
    sal = p.get("salary") if isinstance(p.get("salary"), dict) else {}
    pay = ""
    if sal.get("min") or sal.get("max"):
        pay = f"{sal.get('min') or ''}-{sal.get('max') or ''} {sal.get('currency') or ''} per {sal.get('period') or ''}"
    locs = p.get("locations") if isinstance(p.get("locations"), list) else []
    rows = [
        ("Apply link", p.get("apply_url") or p.get("source_url")),
        ("Seniority", p.get("seniority")),
        ("Years of experience", p.get("seniority_years")),
        ("Internship", p.get("internship")),
        ("Work type", p.get("work_type")),
        ("Workplace", p.get("work_place")),
        ("Locations", "; ".join(_short(x) for x in locs)),
        ("Remote countries", p.get("remote_countries")),
        ("Education", p.get("education")),
        ("Visa sponsorship", p.get("visa_sponsorship")),
        ("Work authorization", p.get("work_authorization")),
        ("Security clearance required", p.get("security_clearance")),
        ("Salary", " ".join(pay.split())),
    ]
    return [f"{k}: {_short(v)}" for k, v in rows if _short(v)]


def describe(listing: Listing) -> str:
    """The posting's full text from its VueJobs page (one request), with its listed facts; hidden text flagged."""
    slug = _post_slug(listing.url) or listing.source_ref or ""
    if not _POST.match(f"/jobs/{slug}"):
        raise ValueError(f"bad vuejobs posting slug: {slug!r}")
    page, _ = get_text(JOB_URL + quote(slug, safe=""))
    p = _post(page, slug)
    if p is None:
        text, hidden = html_to_text(page)
        return mark_hidden(text, hidden)[:MAX_DESC]
    parts, hidden = ["\n".join(_facts(p))], []
    for key in ("about_role", "description"):
        if isinstance(p.get(key), str) and p[key].strip():
            text, h = html_to_text(p[key])
            parts.append(text)
            hidden += h
    return mark_hidden("\n\n".join(x for x in parts if x), hidden)[:MAX_DESC]


def board_of(url: str) -> str | None:
    """'posts' for a vuejobs.com job or feed link (the whole feed is one board), else None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or not _is_vuejobs(host):
        return None
    if _post_slug(url) or p.path.rstrip("/") in ("/jobs", "/feed", "/feed/posts"):
        return SLUG
    return None


BOARD = register_board(
    BoardFeed(
        ats="vuejobs",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example=SLUG,
        terms="Public RSS feed linked from vuejobs.com ('Subscribe to our RSS feed'); no auth or API terms. robots.txt "
        "disallows /feed (and all of app.vuejobs.com) for crawlers, so poll it like a feed reader: one request "
        "per run, at most daily. Posting pages (/jobs/...) are allowed and used by describe(). Keep the VueJobs "
        "posting link; the terms-of-use page was unavailable (HTTP 500) when checked on 2026-10-30.",
        hosts=("app.vuejobs.com", "vuejobs.com"),
    )
)
