"""Arbeitnow job board API: a free, keyless feed of jobs that Arbeitnow gathers from employers' ATSs
(Greenhouse, SmartRecruiters, Join, Teamtailor, Recruitee, Comeet). Mostly Europe (Germany above all),
newest first, 300 per page, updated hourly. Each row has the full HTML description, so describe() isn't
needed. Links point at the posting on arbeitnow.com/.co.uk/.fr/.nl/.ch. Locations are often bare cities,
so the country hint comes from the posting's country site (.com is Germany, Arbeitnow's home market) unless
the location names a country; appa's location filter then drops the non-US ones.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from appa.core.locations import outside_us
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 50
PAUSE = 1.5  # the API allows about 50 requests a minute; stay well under it
MAX_DESC = 100_000

_LEVELS = (
    ("intern", "intern"),
    ("working student", "intern"),
    ("werkstudent", "intern"),
    ("student", "intern"),
    ("étudiant", "intern"),
    ("no experience", "entry"),
    ("aucune expérience", "entry"),
    ("berufseinstieg", "entry"),
    ("entry", "entry"),
    ("junior", "entry"),
    ("graduate", "entry"),
    ("mid-senior", "mid"),
    ("senior", "senior"),
    ("teamleitung", "senior"),
    ("lead", "senior"),
    ("berufserfahren", "mid"),
    ("experienced", "mid"),
    ("expérimenté", "mid"),
    ("mid", "mid"),
)
_TYPES = (
    ("intern", "internship"),
    ("convention de stage", "internship"),
    ("stage", "internship"),
    ("part time", "part_time"),
    ("part-time", "part_time"),
    ("parttime", "part_time"),
    ("teilzeit", "part_time"),
    ("unbefristet", "full_time"),
    ("permanent", "full_time"),
    ("indefinite", "full_time"),
    ("open-ended", "full_time"),
    ("vast contract", "full_time"),
    ("indéterminée", "full_time"),
    ("contract", "contract"),
    ("freelance", "contract"),
    ("full time", "full_time"),
    ("fulltime", "full_time"),
    ("vollzeit", "full_time"),
)
_ANYWHERE = re.compile(r"(remote( job)?|home ?office|poste à distance|télétravail|anywhere)?", re.I)
_SITES = {
    "www.arbeitnow.com": "Germany",
    "www.arbeitnow.co.uk": "United Kingdom",
    "www.arbeitnow.fr": "France",
    "www.arbeitnow.nl": "Netherlands",
    "www.arbeitnow.ch": "Switzerland",
}
_COUNTRIES = {
    "de": "Germany",
    "deutschland": "Germany",
    "germany": "Germany",
    "nl": "Netherlands",
    "nederland": "Netherlands",
    "netherlands": "Netherlands",
    "ch": "Switzerland",
    "schweiz": "Switzerland",
    "suisse": "Switzerland",
    "svizzera": "Switzerland",
    "switzerland": "Switzerland",
    "fr": "France",
    "france": "France",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "united kingdom": "United Kingdom",
    "at": "Austria",
    "österreich": "Austria",
    "austria": "Austria",
    "be": "Belgium",
    "belgium": "Belgium",
    "es": "Spain",
    "spain": "Spain",
    "it": "Italy",
    "italy": "Italy",
    "ie": "Ireland",
    "ireland": "Ireland",
    "pl": "Poland",
    "poland": "Poland",
}


def _date(ts) -> datetime | None:
    """A unix timestamp from the API, as a UTC datetime (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _first(types: list[str], table: tuple) -> str:
    """The first label in `table` whose keyword appears in any of the row's job types, else ""."""
    low = [t.lower() for t in types]
    return next((label for word, label in table if any(word in t for t in low)), "")


def _place(location: str, url: str, remote: bool) -> tuple[str, str]:
    """The location (trailing country codes spelled out, so "Darmstadt, DE" isn't read as Delaware) and a country
    hint."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    named = _COUNTRIES.get(parts[-1].lower(), "") if parts else ""
    if named and len(parts[-1]) == 2:
        location = ", ".join(parts[:-1] + [named])
    if named:
        return location, named
    if len(parts) >= 3:
        return location, parts[-1]  # "City, Region, Country"
    if remote and _ANYWHERE.fullmatch(location.strip()) and not outside_us(location):
        return location, ""  # "Remote" alone: don't guess where
    return location, _SITES.get((urlparse(url).hostname or "").lower(), "")


def _listing(row: dict) -> Listing | None:
    """A Listing from one API row, or None when the row is malformed."""
    if not isinstance(row, dict):
        return None
    url, company, title = str(row.get("url") or ""), str(row.get("company_name") or ""), str(row.get("title") or "")
    if not (url.startswith(("http://", "https://")) and company.strip() and title.strip()):
        return None
    types = [str(t) for t in row.get("job_types") or [] if isinstance(t, str)]
    tags = [str(t) for t in row.get("tags") or [] if isinstance(t, str) and t.lower() != "remote"]
    location, country = _place(str(row.get("location") or ""), url, row.get("remote") is True)
    hints = {
        "remote": "yes" if row.get("remote") is True else "no",
        "category": ", ".join(tags),
        "level": _first(types, _LEVELS),
        "employment_type": _first(types, _TYPES),
        "country": country,
    }
    text, hidden = html_to_text(str(row.get("description") or ""))
    slug = str(row.get("slug") or "")
    posted = _date(row.get("created_at"))
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=location[:500],
            posted_at=posted.date() if posted else None,
            description=mark_hidden(text, hidden)[:MAX_DESC],
            ats_job_id=slug if 0 < len(slug) <= 100 else None,
            source_ref=slug[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Newest postings first, page by page until the feed ends, opts.since is passed or opts.max_results is hit."""
    out: list[Listing] = []
    seen: set[str] = set()  # urls and slugs: the same job is often mirrored on two country sites
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        body = get_json(URL, params={"page": page})
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        older = False
        for x in map(_listing, rows):
            if x is None or x.url in seen or (x.ats_job_id and x.ats_job_id in seen):
                continue
            if opts.since and x.posted_at and x.posted_at < opts.since:
                older = True  # rows are ordered by created_at, newest first
                continue
            seen.update(v for v in (x.url, x.ats_job_id) if v)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if older or not (body.get("links") or {}).get("next"):
            break
    return out


FEED = register_list(
    ListFeed(
        name="arbeitnow",
        description="Arbeitnow job board API: ATS-sourced jobs, mostly Europe/Germany, with full descriptions, hourly",
        fetch=fetch,
        terms="Free public API, no key; 'please do not abuse', a link back to arbeitnow.com is appreciated; "
        "Arbeitnow.com terms of service apply. About 50 requests/minute.",
    )
)
