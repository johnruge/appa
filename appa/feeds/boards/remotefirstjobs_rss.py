"""RemoteFirstJobs: a high-volume remote job board with public RSS feeds (https://remotefirstjobs.com/rss).

GET https://remotefirstjobs.com/rss/jobs.rss returns the newest 100 postings site-wide (about 6 hours' worth;
the feed refreshes every 10 minutes, CDN-cached for up to an hour), and every jobs page has a matching feed at
/rss/jobs/<slug>.rss (e.g. software-development, entry-level, backend, python), also capped at 100. There is
no pagination (page/offset parameters are ignored), so poll every 2-4 hours to miss nothing. The board "slug" is
the feed: "all" for the site-wide feed, else the jobs-page slug.

Each RSS 2.0 item has title ("{Job Title} at {Company}", split on the last " at "), link
(https://remotefirstjobs.com/companies/{company}/jobs/{job-slug}-{id}), guid ("{job-slug}-{id}"), pubDate and
the full HTML description. The numeric id is the posting's stable ID. There are no location fields, so the
remote/country/level hints come only from explicit phrases in the title and description. The employer's apply
link is behind a Cloudflare Turnstile check and a robots-disallowed /a/ path, so it is never resolved; ATS links
written in the description go into hints["links"] for board discovery. www.remotefirstjobs.com 301s to the apex.

Terms (rss page): the feeds are free and public, credit RemoteFirstJobs with a link, and don't submit their jobs
to LinkedIn Jobs, Google Jobs, Jooble or similar. robots.txt allows /rss for all agents.
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

BASE = "https://remotefirstjobs.com/rss/jobs"
HOSTS = ("remotefirstjobs.com", "www.remotefirstjobs.com")
ALL = "all"
MAX_DESC = 100_000
_MAX_HINT = 300
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_ID = re.compile(r"-(\d{1,20})$")
_POSTING = re.compile(r"^/companies/[A-Za-z0-9][A-Za-z0-9._-]{0,199}/jobs/[A-Za-z0-9][A-Za-z0-9._-]{0,299}/?$")
_PAGE = re.compile(r"^/(?:rss/)?jobs/([A-Za-z0-9-]{1,80})(?:\.rss)?/?$")
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_SWE = re.compile(
    r"\b(software|developer|engineer|engineering|programmer|swe|sre|devops|full[- ]?stack|"
    r"front[- ]?end|back[- ]?end|mobile|ios|android|machine learning|ml|ai|data|analyst|"
    r"infrastructure|cloud|sdet|qa|test automation|firmware|embedded|python|java|golang|react|"
    r"security|researcher|technical|it support|systems?|network|database)\b",
    re.I,
)
_NOT_SWE = re.compile(r"\b(sales|account executive|recruit\w*|marketing)\b", re.I)
_ENG = re.compile(r"\b(engineer|developer)\b", re.I)  # "Sales Engineer" stays: appa's own filters decide
_US = re.compile(
    r"\b(?:US|U\.S\.|USA|United States)[- ]?(?:only|based|residents?)\b|"
    r"\banywhere in the (?:U\.S\.|US|USA|United States)\b|"
    r"\bremote,? (?:in|within) (?:the )?(?:U\.S\.|US|USA|United States)\b|"
    r"\b(?:remote|located)[,:]? \(?(?:US|U\.S\.|USA|United States)\)?(?![\w.])|"
    r"\((?:US|U\.S\.|USA|United States)\)",
    re.I,
)
_NEGATION = re.compile(r"\b(?:not|no|non|outside|excluding|except)\b|n't\b", re.I)
_ONLY = re.compile(r"\(([^()]{1,80})\bonly\)", re.I)  # "(Brazil and Argentina Only)"
_US_WORD = re.compile(r"\b(?:US|U\.S\.?|USA|United States|America)\b", re.I)
_CITIZEN = re.compile(
    r"\b(?:must be|requires?|required to be)\b[^.]{0,40}\b(?:U\.S\.|US|United States) citizen|"
    r"\b(?:U\.S\.|US|United States) citizenship (?:is )?required\b|"
    r"\b(?:active|current)\s+(?:secret|top secret|TS/SCI|TS)\b[^.]{0,20}\bclearance\b",
    re.I,
)
_SENTENCE = r"(?:[^.\n]|\.(?!\s))*"  # a run of text up to a sentence end ("U.S." stays whole)
_SPONSOR = re.compile(rf"{_SENTENCE}\bsponsor\w*\b{_SENTENCE}", re.I)
_VISA = re.compile(r"\b(visa|h-?1b|immigration|work authori[sz]ation|authorized to work)\b", re.I)
_SALARY = re.compile(
    r"\$\s?\d[\d,.]*\s?[kK]?\s?(?:-|–|to)\s?\$?\s?\d[\d,.]*\s?[kK]?(?:\s?(?:/|per )\s?(?:hour|hr|year|yr))?"
)


def _check(slug: str) -> str:
    """The slug, lowercased, if it is a safe RemoteFirstJobs feed slug; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug.lower()) or slug.endswith("-"):
        raise ValueError(f"bad remotefirstjobs slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The RSS URL for a feed slug ("all" is the site-wide feed)."""
    slug = _check(slug)
    return f"{BASE}.rss" if slug == ALL else f"{BASE}/{quote(slug, safe='')}.rss"


def _is_rfj(url: str) -> bool:
    """True when the link is on remotefirstjobs.com (apex or www) itself."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".") in HOSTS
    except (AttributeError, ValueError):
        return False


def _day(v: str | None) -> date | None:
    """An RFC 822 RSS date as a date (None if missing or invalid)."""
    try:
        return parsedate_to_datetime(v or "").date()
    except (TypeError, ValueError, IndexError):
        return None


def is_swe(title: str) -> bool:
    """True when the title could be a software / data / security / IT role (kept loose: appa filters later)."""
    return bool(_SWE.search(title)) and not (_NOT_SWE.search(title) and not _ENG.search(title))


def _level(title: str) -> str:
    """The seniority the title states, or ''."""
    t = title.lower()
    if re.search(r"\bintern(ship)?\b", t):
        return "intern"
    if re.search(r"\bnew[- ]?grad(uate)?\b", t):
        return "new_grad"
    if re.search(r"\b(junior|jr\.?|entry[- ]level)\b", t):
        return "entry"
    if re.search(r"\b(senior|sr\.?|staff|principal)\b", t):
        return "senior"
    if re.search(r"\b(?i:engineer|developer|scientist|analyst)\s+(?:I|1)\b", title):
        return "entry"
    return ""


def _employment(title: str) -> str:
    """The employment type the title states, or ''."""
    t = title.lower()
    if re.search(r"\bintern(ship)?\b", t):
        return "internship"
    if re.search(r"\bcontract(or)?\b|\bfreelance\b", t):
        return "contract"
    if re.search(r"\bpart[- ]time\b", t):
        return "part_time"
    return ""


def _us(title: str, text: str) -> bool:
    """True when the title or description says the job is US-based, skipping negated phrases ("not considering
    US based candidates") and titles limited to other countries ("(Brazil and Argentina Only)")."""
    only = _ONLY.search(title)
    if only and not _US_WORD.search(only.group(1)):
        return False
    both = f"{title}\n{text}"
    for m in _US.finditer(both):
        before = re.split(r"[.!?\n]\s", both[max(0, m.start() - 40) : m.start()])[-1]
        if not _NEGATION.search(before):
            return True
    return False


def _links(raw_html: str) -> str:
    """Non-RemoteFirstJobs http(s) links in the description, space separated, never cut mid-URL."""
    out: list[str] = []
    for h in _HREF.findall(raw_html):
        h = "".join(h.split())
        if not h.lower().startswith(("http://", "https://")) or _is_rfj(h) or h in out:
            continue
        if len(" ".join(out + [h])) > _MAX_HINT or len(out) == 10:
            break
        out.append(h)
    return " ".join(out)


def _hints(title: str, text: str, raw: str) -> dict[str, str]:
    """Facts the title or description states outright (see HINT_KEYS)."""
    text = re.sub(r"[ \t]*\n\s*:", ":", text)  # "Visa sponsorship\n: Available" is one sentence
    both = f"{title}\n{text}"
    sponsor = next((m for m in _SPONSOR.finditer(text) if _VISA.search(m.group(0))), None)
    salary = _SALARY.search(both)
    return {
        "remote": "hybrid" if re.search(r"\bhybrid\b", title, re.I) else "yes",
        "country": "United States" if _us(title, text) else "",
        "citizenship_required": "yes" if _CITIZEN.search(text) else "",
        "sponsorship": sponsor.group(0).strip()[:_MAX_HINT] if sponsor else "",
        "salary": salary.group(0).strip() if salary else "",
        "level": _level(title),
        "employment_type": _employment(title),
        "links": _links(raw),
    }


def _listing(it) -> Listing:
    """One RSS item as a Listing (ValueError when a required field is missing or bad)."""
    link = (it.findtext("link") or "").strip()
    if not _is_rfj(link) or not _POSTING.match(urlparse(link).path):
        raise ValueError("not a remotefirstjobs posting link")
    title, sep, company = (it.findtext("title") or "").strip().rpartition(" at ")
    if not sep:
        raise ValueError("title has no ' at Company'")
    guid = (it.findtext("guid") or "").strip() or urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]
    m = _ID.search(guid)
    raw = it.findtext("description") or ""
    text, hidden = html_to_text(raw) if raw else ("", [])
    hints = _hints(title, text, raw)
    location = "Remote" + (" (United States)" if hints["country"] else "")
    return Listing(
        company=company,
        title=title,
        url=link,
        location=location,
        posted_at=_day(it.findtext("pubDate")),
        description=mark_hidden(text, hidden)[:MAX_DESC],
        ats_job_id=m.group(1) if m else None,
        source_ref=guid[:300],
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """The newest 100 postings in this feed (one request, no paging), software roles only, newest first."""
    opts = opts or FeedOptions()
    root = ET.fromstring(get_bytes(feed_url(slug)))
    out, seen = [], set()
    for it in root.iter("item"):
        try:
            x = _listing(it)
        except ValueError as e:
            log.debug("skipping remotefirstjobs item: %s", e)
            continue
        if (x.ats_job_id or x.url) in seen or not is_swe(x.title):
            continue
        seen.add(x.ats_job_id or x.url)
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The feed slug a RemoteFirstJobs link belongs to: "all" for postings and the main feed, else the
    /jobs/<slug> page or /rss/jobs/<slug>.rss feed; None for anything else."""
    if not _is_rfj(url):
        return None
    path = urlparse(url.strip()).path
    if path.rstrip("/") in ("/jobs", "/rss/jobs.rss") or _POSTING.match(path):
        return ALL
    m = _PAGE.match(path)
    if not m:
        return None
    try:
        return _check(m.group(1))
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="remotefirstjobs_rss",
        fetch=fetch,
        board_of=board_of,
        example=ALL,
        terms="Public RSS feeds (remotefirstjobs.com/rss): free to use with a link back to RemoteFirstJobs; don't "
        "submit "
        "their jobs to LinkedIn Jobs, Google Jobs, Jooble or similar. Newest 100 per feed, refreshed every 10 min, "
        "no paging: poll every 2-4 hours. Apply links sit behind a Turnstile check and a robots-disallowed /a/ "
        "path, so keep the RemoteFirstJobs posting link and don't automate the apply redirect.",
        hosts=("remotefirstjobs.com",),
    )
)
