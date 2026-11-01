"""Himalayas: a remote-jobs board with a free public JSON API (no key needed). appa uses its search
endpoint, one search per query, newest first, filtered to the US (worldwide-friendly jobs included),
20 jobs a page until a page comes back empty.
Jobs past their expiryDate are dropped, as the terms require. Each job carries the full HTML description, seniority,
salary and location restrictions. An optional free key (HIMALAYAS_API_KEY) gives a private rate limit.
Docs: https://himalayas.app/api
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://himalayas.app/jobs/api/search"
KEY_ENV = "HIMALAYAS_API_KEY"  # optional: higher rate limits; the API works without one
MAX_PAGES = 50  # per query (the API caps search at ~250 pages)
PAUSE = 1.0  # seconds between requests; the keyless allowance is shared
LEVELS = {
    "entry-level": "entry",
    "mid-level": "mid",
    "senior": "senior",
    "manager": "senior",
    "director": "senior",
    "executive": "senior",
}
TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "contractor": "contract",
    "temporary": "temporary",
    "intern": "internship",
    "volunteer": "volunteer",
}
US_NAMES = {"united states", "united states of america", "usa", "us", "u.s."}


def _items(v) -> list[str]:
    """A list field from the API as a list of strings (tolerates a bare string or null)."""
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)] if v not in (None, "") else []


def _date(ts) -> date | None:
    """A unix timestamp (seconds) as a UTC date, or None if missing or invalid."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _salary(row: dict) -> str:
    """The posted pay range as text, e.g. '65000-150000 USD/annual' ('' when there is none)."""
    lo, hi = row.get("minSalary"), row.get("maxSalary")
    if lo in (None, "") and hi in (None, ""):
        return ""
    rng = str(lo) if hi in (None, "", lo) else f"{lo}-{hi}" if lo not in (None, "") else str(hi)
    return f"{rng} {row.get('currency') or ''}/{row.get('salaryPeriod') or 'annual'}".replace(" /", "/")


def _job_id(guid: str) -> str | None:
    """'company/job-slug' from a job's guid on himalayas.app, or None for anything else."""
    p = urlparse(guid)
    parts = [x for x in p.path.split("/") if x]
    if p.hostname != "himalayas.app" or len(parts) != 4 or parts[0] != "companies" or parts[2] != "jobs":
        return None
    jid = f"{parts[1]}/{parts[3]}"
    return jid if len(jid) <= 100 else None


def _is_us(name: str) -> bool:
    """True when a country name means the United States."""
    return name.strip().lower() in US_NAMES


def _us_open(row: dict) -> bool:
    """True when a raw job is open to US candidates (no country restriction, or the US is listed)."""
    countries = _items(row.get("locationRestrictions")) if isinstance(row, dict) else []
    return not countries or any(map(_is_us, countries))


def _countries_hint(countries: list[str]) -> str:
    """The country hint: the US first, then whole names until ~280 chars, then '+N more'."""
    us = [c for c in countries if _is_us(c)][:1]
    names = us + [c for c in countries if not _is_us(c)]
    out: list[str] = []
    for i, c in enumerate(names):
        if len(", ".join(out + [c])) > 280:
            return ", ".join(out) + f" +{len(names) - i} more"
        out.append(c)
    return ", ".join(out)


def _location(countries: list[str]) -> str:
    """A short location: 'Remote', 'Remote (Canada, Mexico)', 'Remote (United States + 55 more)'."""
    if not countries:
        return "Remote"
    if len(countries) <= 3:
        return f"Remote ({', '.join(countries)})"[:500]
    if any(map(_is_us, countries)):
        return f"Remote (United States + {len(countries) - 1} more)"
    return f"Remote ({len(countries)} countries)"


def _now() -> float:
    """The current unix time (a seam so tests can pin the clock)."""
    return time.time()


def _expired(row: dict, now: float) -> bool:
    """True when the job's expiryDate (unix seconds) is in the past (terms: remove expired jobs)."""
    try:
        return int(row.get("expiryDate")) < now
    except (TypeError, ValueError):
        return False


def _listing(row: dict, now: float | None = None) -> Listing | None:
    """A Listing from one search result, or None when the row is malformed or expired."""
    if not isinstance(row, dict) or _expired(row, _now() if now is None else now):
        return None
    url = str(row.get("applicationLink") or row.get("guid") or "")  # terms: use applicationLink unchanged
    countries = _items(row.get("locationRestrictions"))
    etype = str(row.get("employmentType") or "")
    levels = list(dict.fromkeys(LEVELS[s.lower()] for s in _items(row.get("seniority")) if s.lower() in LEVELS))
    if etype.lower() == "intern":
        levels = ["intern"]
    text, hidden = html_to_text(str(row.get("description") or ""))
    hints = {
        "remote": "yes",
        "country": _countries_hint(countries) or "worldwide",
        "level": ", ".join(levels),
        "employment_type": TYPES.get(etype.lower(), etype),
        "category": ", ".join(_items(row.get("parentCategories"))),
        "salary": _salary(row),
    }
    try:
        return Listing(
            company=str(row.get("companyName") or ""),
            title=str(row.get("title") or ""),
            url=url,
            location=_location(countries),
            posted_at=_date(row.get("pubDate")),
            description=mark_hidden(text, hidden)[:100_000],
            ats_job_id=_job_id(str(row.get("guid") or "")),
            source_ref=str(row.get("guid") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def _all_old(rows: list, since: date) -> bool:
    """True when every row on a page that has a pubDate is older than since (and at least one does)."""
    dates = [d for d in (_date(r.get("pubDate")) for r in rows if isinstance(r, dict)) if d]
    return bool(dates) and all(d < since for d in dates)


def fetch(opts: FeedOptions) -> list[Listing]:
    """Newest remote jobs for each query (US-open when opts.us_only), deduped, stopping at opts.since."""
    key = os.environ.get(KEY_ENV)
    headers = {"Authorization": f"Bearer {key}"} if key else None
    out: dict[str, Listing] = {}
    first = True
    for q in opts.queries:
        for page in range(1, MAX_PAGES + 1):
            if not first:
                time.sleep(PAUSE)
            first = False
            params = {"q": q, "sort": "recent", "page": page}
            if opts.us_only:
                params["country"] = "US"
            data = get_json(URL, params=params, headers=headers)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not rows or not isinstance(rows, list):
                break
            now = _now()
            for row in rows:
                if opts.us_only and not _us_open(row):
                    continue
                x = _listing(row, now)
                if x is None or (opts.since and x.posted_at and x.posted_at < opts.since):
                    continue
                out.setdefault(x.ats_job_id or x.url, x)
            if len(out) >= opts.max_results or (opts.since and _all_old(rows, opts.since)):
                break
        if len(out) >= opts.max_results:
            break
    return list(out.values())[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="himalayas",
        description="Himalayas remote-jobs API: keyword search, US-open remote roles with full descriptions",
        fetch=fetch,
        terms="Free public API (https://himalayas.app/api). Keep applicationLink unchanged, credit Himalayas, "
        "don't republish to other boards/aggregators, drop expired jobs (expiryDate is enforced). Paged "
        "search (page=) is documented in the API docs; note robots.txt 'Disallow: /jobs*&page=' also matches "
        "it, so this relies on the API docs, not robots. Keyless calls share one rate "
        "allowance; optional free key HIMALAYAS_API_KEY gives 60/min, 10k/day. Data refreshes daily.",
    )
)
