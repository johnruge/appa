"""CryptoJobsList (cryptojobslist.com), a web3/crypto/fintech jobs board, through its public RSS feed.

https://cryptojobslist.com/rss (linked from the site) redirects to https://api.cryptojobslist.com/jobs.rss, which
returns the 100 newest postings (~750 KB, no key, no paging; cached for 30 minutes, so poll every 6-12 h). Each
<item> has the job title, the job page as link/guid (https://cryptojobslist.com/jobs/{title-location}-at-{company}),
the company in dc:creator, a free-text media:location ('United States', 'Remote', 'London', or empty) and the full
HTML posting in description, which starts with a 'Tags:' paragraph of category links (/engineering, /remote,
/full-time, /senior...). Items have no date, so posted_at stays empty (appa uses first-seen time).

The board's "slug" is "all" (every posting) or a tag such as "engineering", which keeps only postings carrying
that tag. The job page's link slug is the posting's identity. With opts.us_only, postings whose location names
no US place and that aren't remote are dropped; postings with no location are kept. The site's pages sit
behind a bot challenge and its terms page can't be read automatically, so only the feed is used.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from defusedxml import ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED = "https://api.cryptojobslist.com/jobs.rss"
ALL = "all"
HOSTS = ("cryptojobslist.com", "www.cryptojobslist.com")
DC = "{http://purl.org/dc/elements/1.1/}"
MEDIA = "{http://search.yahoo.com/mrss/}"
MAX_DESC = 100_000
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_JOB_PATH = re.compile(r"^/jobs/([a-z0-9][a-z0-9-]{0,199})/?$")
_TAGS = re.compile(r"<p>\s*Tags:((?:\s*(?:•\s*)?<a\s[^>]*>[^<]*</a>)+)", re.I)
_TAG_HREF = re.compile(r"""href=["']https://cryptojobslist\.com/([a-z0-9-]+)["']""")
_OG_IMG = re.compile(r"<img\s[^>]*webfeedsFeaturedVisual[^>]*>", re.I)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
_JOBLIKE = re.compile(r"job|career|apply|greenhouse|lever\.co|ashbyhq|workable|myworkdayjobs|smartrecruiters", re.I)
_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
    "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
)
_CITIES = "nyc|san francisco|bay area|silicon valley|chicago|boston|seattle|los angeles|austin|miami|denver|atlanta"
_US = re.compile(rf"united states|\busa?\b|u\.s\.|\bamericas?\b|{_STATES}|{_CITIES}", re.I)
_STATE_CODE = re.compile(
    r",\s*(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|"
    r"S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b"
)
_OPEN = re.compile(r"remote|anywhere|worldwide|global", re.I)
_LEVELS = {"internship": "intern", "junior-level": "entry", "senior": "senior", "lead-level": "senior"}
_TYPES = {"full-time": "full_time", "part-time": "part_time", "contract": "contract", "internship": "internship"}
_NOT_CATEGORY = {"web3", "remote", *_LEVELS, *_TYPES}


def _check(slug: str) -> str:
    """The slug ('all' or a tag name), if it is safe; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"bad cryptojobslist slug: {slug!r}")
    return slug


def parse(raw: bytes) -> list:
    """The <item> elements of the feed."""
    return ET.fromstring(raw).findall("./channel/item")


def _job_slug(link: str) -> str:
    """The job page's slug from a cryptojobslist.com/jobs/{slug} link; else ValueError."""
    p = urlparse(link.strip())
    m = _JOB_PATH.fullmatch(p.path)
    if p.scheme != "https" or (p.hostname or "").lower() not in HOSTS or not m:
        raise ValueError(f"not a cryptojobslist job link: {link[:100]!r}")
    return m.group(1)


def _split(raw: str) -> tuple[list[str], str]:
    """(tag slugs, the posting HTML without the tags paragraph and the banner image)."""
    m = _TAGS.search(raw)
    tags = list(dict.fromkeys(_TAG_HREF.findall(m.group(1)))) if m else []
    body = raw[: m.start()] + raw[m.end() :] if m else raw
    return tags, _OG_IMG.sub("", body)


def _links(raw: str) -> str:
    """Job/careers links outside CryptoJobsList in the posting, space separated (for board discovery)."""
    out: list[str] = []
    for h in _HREF.findall(raw):
        host = (urlparse(h).hostname or "").lower()
        if h.lower().startswith(("http://", "https://")) and host not in HOSTS and _JOBLIKE.search(h) and h not in out:
            out.append(h)
    return " ".join(out[:5])


def _hints(tags: list[str], location: str, raw: str) -> dict[str, str]:
    """What the tags and location state about the posting."""
    remote = "remote" in tags or bool(re.search(r"\bremote\b", location, re.I))
    return {
        "remote": "yes" if remote else "",
        "level": next((_LEVELS[t] for t in tags if t in _LEVELS), ""),
        "employment_type": next((_TYPES[t] for t in tags if t in _TYPES), ""),
        "category": ", ".join(t for t in tags if t not in _NOT_CATEGORY),
        "country": "US" if re.search(r"united states|\busa\b|u\.s\.", location, re.I) else "",
        "links": _links(raw),
    }


def _listing(item) -> tuple[Listing, list[str]]:
    """One <item> as a Listing plus its tags (ValueError when a required field is missing or bad)."""

    def get(tag: str) -> str:
        """The element's text, stripped."""
        return (item.findtext(tag) or "").strip()

    jid = _job_slug(get("link") or get("guid"))
    raw = get("description")
    tags, body = _split(raw)
    text, hidden = html_to_text(body)
    location = get(f"{MEDIA}location")
    return Listing(
        company=get(f"{DC}creator"),
        title=get("title"),
        url=f"https://cryptojobslist.com/jobs/{jid}",
        location=location,
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats="cryptojobslist",
        ats_job_id=jid[:100],
        source_ref=jid[:100],
        hints=_hints(tags, location, raw),
    ), tags


def _us_ok(x: Listing) -> bool:
    """True unless the posting names only non-US places and isn't remote."""
    loc = x.location
    return not loc or x.hints.get("remote") == "yes" or any(r.search(loc) for r in (_US, _STATE_CODE, _OPEN))


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """The newest postings (all, or those with this tag), one request; the feed has no pages."""
    slug, opts = _check(slug), opts or FeedOptions()
    out, seen = [], set()
    for item in parse(get_bytes(FEED)):
        try:
            x, tags = _listing(item)
        except ValueError as e:
            log.debug("skipping cryptojobslist item: %s", e)
            continue
        if x.ats_job_id in seen or (slug != ALL and slug not in tags) or (opts.us_only and not _us_ok(x)):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """'all' for a CryptoJobsList home, feed or job page link (the site is one board); None otherwise."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    if host == "api.cryptojobslist.com":
        return ALL if p.path == "/jobs.rss" else None
    if host not in HOSTS:
        return None
    return ALL if p.path in ("", "/", "/rss", "/rss/", "/jobs", "/jobs/") or _JOB_PATH.fullmatch(p.path) else None


BOARD = register_board(
    BoardFeed(
        ats="cryptojobslist",
        fetch=fetch,
        board_of=board_of,
        example=ALL,
        terms="Public RSS feed the site links (no key); only the 100 newest items, cached 30 min, poll every 6-12 h. "
        "Site pages are behind a bot challenge, so job pages are never fetched.",
        hosts=("api.cryptojobslist.com", "cryptojobslist.com"),
    )
)
