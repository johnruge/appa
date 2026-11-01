"""Personio job boards, through the public positions XML feed (no key).

GET https://{slug}.jobs.personio.de/xml returns every open position on a company's Personio career page
in one response: title, offices, department, employment type, seniority, created date, optional salary
and the description sections (HTML in CDATA). The same boards answer on {slug}.jobs.personio.com.
Personio documents this feed for showing a company's open positions on other sites (developer.personio.de,
"Retrieving open positions"). No `language` parameter is sent: `language=en` blanks the description of
every position the company wrote only in German, while the default returns each in its own language.
A board whose feed is switched off returns an empty <workzag-jobs/>; an unknown slug is a 404.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from defusedxml import ElementTree

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under jobs.personio.de
_DOMAINS = (".jobs.personio.de", ".jobs.personio.com")
_NOT_BOARDS = {"www", "api", "app", "static", "assets", "cdn"}
_LEVELS = {"entry-level": "entry", "student": "intern"}
_TYPES = {
    "intern": "internship",
    "trainee": "trainee",
    "working_student": "working_student",
    "freelance": "contract",
    "temporary": "contract",
}
_SCHEDULES = {"full-time": "full_time", "part-time": "part_time", "full-or-part-time": "full_time, part_time"}
_PERIODS = {"yearly": "year", "monthly": "month", "hourly": "hour", "weekly": "week", "daily": "day"}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Personio subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad personio slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The positions XML feed URL for a board."""
    return f"https://{quote(_check(slug), safe='')}.jobs.personio.de/xml"


def _text(el, tag: str) -> str:
    """Stripped text of a child element, or ''."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _day(v: str) -> date | None:
    """'2026-10-30T15:26:21+00:00' -> date, or None."""
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _offices(p) -> list[str]:
    """The main office then any additional offices, without repeats."""
    names = [_text(p, "office")] + [(o.text or "").strip() for o in p.findall("additionalOffices/office")]
    return list(dict.fromkeys(n for n in names if n))


def _num(v: str) -> str:
    """'36000.00' -> '36000'."""
    return v[:-3] if v.endswith(".00") else v


def _salary(p) -> str:
    """'36000-50000 EUR/year' from salaryInformation, or ''."""
    s = p.find("salaryInformation")
    if s is None:
        return ""
    rng = "-".join(_num(v) for v in (_text(s, "min"), _text(s, "max")) if v.strip("0."))
    if not rng:
        return ""
    period = _PERIODS.get(_text(s, "type"), _text(s, "type"))
    unit = _text(s, "currencyCode") + (f"/{period}" if period else "")
    return f"{rng} {unit}".strip()


def _hints(p, offices: list[str]) -> dict[str, str]:
    """The facts the position states, under HINT_KEYS names."""
    etype = _text(p, "employmentType")
    return {
        "level": _LEVELS.get(_text(p, "seniority"), ""),
        "employment_type": _TYPES.get(etype) or _SCHEDULES.get(_text(p, "schedule"), ""),
        "remote": "yes" if any("remote" in o.lower() for o in offices) else "",
        "category": _text(p, "department") or _text(p, "recruitingCategory"),
        "salary": _salary(p),
    }


def _description(p) -> str:
    """Every description section (heading + text), HTML converted and hidden text flagged."""
    parts = []
    for d in p.findall("jobDescriptions/jobDescription"):
        text, hidden = html_to_text(_text(d, "value"))
        body = mark_hidden(text, hidden)
        parts.append("\n".join(x for x in (_text(d, "name"), body) if x))
    return "\n\n".join(x for x in parts if x)[:MAX_DESC]


def _listing(p, slug: str) -> Listing:
    """One <position> as a Listing (ValueError when a required field is missing or bad)."""
    pid = _text(p, "id")
    if not pid.isdigit() or len(pid) > 20:
        raise ValueError(f"bad personio position id: {pid[:30]!r}")
    offices = _offices(p)
    return Listing(
        company=_text(p, "subcompany") or slug,
        title=_text(p, "name"),
        url=f"https://{slug}.jobs.personio.de/job/{pid}",
        location="; ".join(offices)[:500],
        posted_at=_day(_text(p, "createdAt")),
        description=_description(p),
        ats="personio",
        ats_job_id=pid,
        source_ref=pid,
        hints=_hints(p, offices),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open position on this company's Personio board, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    root = ElementTree.fromstring(get_bytes(feed_url(slug)))
    out, seen = [], set()
    for p in root.findall("position"):
        try:
            x = _listing(p, slug)
        except ValueError as e:
            log.debug("skipping personio position on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.jobs.personio.de/.com link, or None (custom career domains aren't recognised)."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    domain = next((d for d in _DOMAINS if host.endswith(d)), None)
    if not domain:
        return None
    label = host[: -len(domain)]
    if "." in label or label in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="personio",
        fetch=fetch,
        board_of=board_of,
        example="1komma5grad",
        terms="Public positions XML feed (no key), documented by Personio for listing a company's open jobs; "
        "one request per board.",
        hosts=("jobs.personio.de", "jobs.personio.com"),
    )
)
