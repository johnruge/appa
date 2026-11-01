"""Python.org's official job board (python.org/jobs), through its public RSS feed (no key).

GET https://www.python.org/jobs/feed/rss/ returns the 20 newest approved postings (no paging parameters;
a few arrive a week, so polling daily is enough). Each item has a title '{Job Title}, {Company}' (split on
the last comma, keeping suffixes like 'Inc.' with the company), link and guid
(https://www.python.org/jobs/{id}/, the human posting page and the stable ID), and a description whose
first line is the location ('Rowlett, TX, United States') followed by the posting as escaped HTML. Items
have no pubDate (or any other date), so posted_at is left None.

The board is global and its location is free text, so with opts.us_only the filter keeps by default: a
posting is dropped only when its location names a non-US country, region or city (UK, Europe, India,
London, ...) and names no US place. Remote, worldwide, anywhere, North America, US timezones and blank
locations are kept. It's one site, not an ATS, so it has a single board
slug, "python.org"; board_of() maps python.org job and feed links to it. robots.txt allows /jobs/; the
feed is published for syndication and the site asks for no key or login.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import defusedxml.ElementTree as ET

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

FEED = "https://www.python.org/jobs/feed/rss/"
SLUG = "python.org"
MAX_DESC = 100_000
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_JOB_PATH = re.compile(r"^/jobs/(\d{1,20})/?$")
_SUFFIX = re.compile(r"^(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|s\.?a|plc|pty ltd|b\.?v)\.?$", re.I)
_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
    "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|hawaii|idaho|"
    "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
    "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
)
_US = re.compile(rf"united states|\bu\.s\.a?\.?(?!\w)|\busa\b|\bus\b|\b({_STATE_NAMES})\b", re.I)
# State codes that are also common country codes (IN India, DE Germany, CA Canada, ...) don't prove "US"
# when a foreign place is named too ('Bangalore, IN', 'Berlin, DE').
_AMBIGUOUS = {
    "IN",
    "DE",
    "CA",
    "IL",
    "CO",
    "AR",
    "PA",
    "MA",
    "ID",
    "GA",
    "AL",
    "MT",
    "SD",
    "TN",
    "SC",
    "NE",
    "MN",
    "ME",
    "LA",
    "VA",
    "MD",
    "NC",
    "KY",
}
_US_CODE = re.compile(r"(?:^|[,(/])\s*(" + "|".join(sorted(set(_STATES) - _AMBIGUOUS)) + r")\s*(?:[,)/]|$|\d)")
_FOREIGN = re.compile(
    r"\b(uk|u\.k\.|united kingdom|england|scotland|wales|ireland|britain|gb|eu|europe|european|emea|apac|asia|"
    r"latam|latin america|south america|africa|middle east|oceania|canada|mexico|brazil|argentina|colombia|chile|"
    r"peru|germany|deutschland|france|spain|portugal|italy|netherlands|belgium|switzerland|austria|poland|czechia|"
    r"czech republic|slovakia|hungary|romania|bulgaria|greece|croatia|serbia|ukraine|russia|sweden|norway|"
    r"denmark|finland|estonia|latvia|lithuania|turkey|israel|egypt|nigeria|kenya|south africa|uae|"
    r"united arab emirates|saudi arabia|qatar|pakistan|india|bangladesh|sri lanka|nepal|china|hong kong|taiwan|"
    r"japan|korea|singapore|malaysia|indonesia|philippines|vietnam|thailand|australia|new zealand|"
    r"london|manchester|edinburgh|dublin|berlin|munich|hamburg|frankfurt|paris|amsterdam|rotterdam|brussels|"
    r"zurich|geneva|vienna|warsaw|krakow|kraków|wroclaw|prague|budapest|bucharest|madrid|barcelona|lisbon|porto|"
    r"rome|milan|stockholm|oslo|copenhagen|helsinki|tallinn|kyiv|kiev|istanbul|tel aviv|dubai|cairo|lagos|"
    r"nairobi|cape town|bangalore|bengaluru|mumbai|delhi|new delhi|hyderabad|pune|chennai|noida|gurgaon|"
    r"gurugram|karachi|lahore|dhaka|beijing|shanghai|shenzhen|tokyo|seoul|sydney|melbourne|auckland|toronto|"
    r"vancouver|montreal|ottawa|calgary|mexico city|guadalajara|sao paulo|são paulo|buenos aires|bogota|"
    r"bogotá|santiago|lima)\b"
    r"|(?:^|,)\s*(gb|fr|es|pt|it|nl|be|ch|at|pl|cz|ro|ua|se|no|dk|fi|ie|br|mx|au|nz|jp|cn|sg|hk|tw|kr|ph|"
    r"my|vn|th|za|ng|ke|eg|ae|pk|bd|lk|tr|gr|hu|bg|hr|rs|ee|lv|lt|sk|si|cl|pe|uy|on|bc|qc|ab)\s*(?:,|$)",
    re.I,
)
_LEVELS = (
    ("intern", "intern"),
    ("junior", "entry"),
    ("entry level", "entry"),
    ("entry-level", "entry"),
    ("new grad", "new_grad"),
    ("senior", "senior"),
    ("mid-level", "mid"),
    ("mid level", "mid"),
)


def _check(slug: str) -> str:
    """The slug, if it is this site's one board; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() != SLUG:
        raise ValueError(f"bad python.org jobs slug: {slug!r}")
    return SLUG


def _host(url: str) -> str:
    """The lowercased host of a link, or "" when it can't be parsed."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return ""


def _split_title(raw: str) -> tuple[str, str]:
    """'Senior Engineer, Acme, Inc.' -> ('Senior Engineer', 'Acme, Inc.'); ValueError without a company."""
    parts = [p.strip() for p in raw.split(",")]
    cut = len(parts) - 1
    if cut > 1 and _SUFFIX.match(parts[cut]):
        cut -= 1
    title, company = ", ".join(parts[:cut]).strip(), ", ".join(parts[cut:]).strip()
    if not title or not company:
        raise ValueError(f"title has no company: {raw[:100]!r}")
    return title, company


def _is_us(location: str) -> bool:
    """Whether the location names a US place (a state, 'United States', 'USA', a non-ambiguous state code)."""
    return bool(_US.search(location) or _US_CODE.search(location))


def _keep_for_us(location: str) -> bool:
    """False only when the location names a non-US place and no US place (keep when unsure)."""
    return _is_us(location) or not _FOREIGN.search(location)


def _hints(title: str, location: str) -> dict[str, str]:
    """Facts the item states: US location, remote/hybrid, a level word in the title."""
    loc, low = location.lower(), title.lower()
    remote = "hybrid" if "hybrid" in loc + low else "yes" if "remote" in loc + low else ""
    level = next((v for k, v in _LEVELS if re.search(rf"\b{k}\b", low)), "")
    return {"country": "United States" if _is_us(location) else "", "remote": remote, "level": level}


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when the link isn't a python.org job link or a field is missing)."""
    link = (it.findtext("link") or "").strip()
    m = _JOB_PATH.match(urlparse(link).path) if _host(link) in ("www.python.org", "python.org") else None
    if not m:
        raise ValueError(f"not a python.org job link: {link[:100]!r}")
    title, company = _split_title((it.findtext("title") or "").strip())
    location, _, body = (it.findtext("description") or "").strip().partition("\n")
    text, hidden = html_to_text(body) if body.strip() else ("", [])
    location = html_to_text(location)[0][:500]
    return Listing(
        company=company,
        title=title,
        url=f"https://www.python.org/jobs/{m.group(1)}/",
        location=location,
        posted_at=None,
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats="python.org",
        ats_job_id=m.group(1),
        source_ref=(it.findtext("guid") or "").strip()[:300] or m.group(1),
        hints=_hints(title, location),
    )


def fetch(slug: str = SLUG, opts: FeedOptions | None = None) -> list[Listing]:
    """The newest Python.org job postings (one request; the feed isn't paged)."""
    _check(slug)
    opts = opts or FeedOptions()
    root = ET.fromstring(get_bytes(FEED))
    channel = root.find("channel")
    out, seen = [], set()
    for it in channel.findall("item") if channel is not None else []:
        try:
            x = _listing(it)
        except ValueError as e:
            log.debug("skipping python.org item: %s", e)
            continue
        if x.ats_job_id in seen or (opts.us_only and not _keep_for_us(x.location)):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """ "python.org" for a python.org job posting or jobs feed link, else None."""
    try:
        parsed = urlparse(url.strip()) if isinstance(url, str) else None
        if parsed is None or parsed.scheme.lower() not in ("http", "https") or parsed.username or parsed.port:
            return None
    except ValueError:
        return None
    if (parsed.hostname or "").lower().rstrip(".") not in ("python.org", "www.python.org"):
        return None
    path = parsed.path
    return SLUG if _JOB_PATH.match(path) or path.rstrip("/") == "/jobs/feed/rss" else None


BOARD = register_board(
    BoardFeed(
        ats="python.org",
        fetch=fetch,
        board_of=board_of,
        example=SLUG,
        terms="Public RSS feed (no key, no paging, 20 newest posts; poll daily); robots.txt allows /jobs/. "
        "Global board: us_only drops only postings that name a non-US place and no US place. No dates, so posted_at "
        "is None.",
        hosts=("www.python.org",),
    )
)
