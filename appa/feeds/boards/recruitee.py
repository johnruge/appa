"""Recruitee job boards, through the public Careers Site API (no key).

GET https://{slug}.recruitee.com/api/offers/ returns every published offer on a company's board in one
response, with the description and requirements HTML, locations, salary, remote/hybrid flags and the
human posting link (careers_url, which may be on the company's own domain). Recruitee documents this API
for showing a company's jobs on other sites (docs.recruitee.com, "Intro to Careers Site API").
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under recruitee.com
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "docs",
    "help",
    "support",
    "blog",
    "status",
    "partners",
    "marketplace",
    "careers",
    "jobs",
    "cdn",
    "static",
    "assets",
    "mail",
    "auth",
    "login",
    "id",
}
_LEVELS = {
    "student_college": "intern",
    "student_high_school": "intern",
    "entry_level": "entry",
    "mid_level": "mid",
    "senior_manager": "senior",
    "executive": "senior",
}
_TYPES = {
    "fulltime": "full_time",
    "parttime": "part_time",
    "internship": "internship",
    "freelance": "contract",
    "contract": "contract",
    "temporary": "temporary",
    "traineeship": "internship",
}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Recruitee subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad recruitee slug: {slug!r}")
    return slug.lower()


def api_url(slug: str) -> str:
    """The Careers Site API offers URL for a board."""
    return f"https://{quote(_check(slug), safe='')}.recruitee.com/api/offers/"


def _day(v) -> date | None:
    """'2026-10-28 13:21:58 UTC' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _location(o: dict) -> tuple[str, list[str]]:
    """(location text, country names) from the offer's locations list, falling back to its location field."""
    parts, countries = [], []
    for loc in o.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        bits = [loc.get(k) for k in ("city", "state", "country") if loc.get(k)]
        if bits:
            parts.append(", ".join(dict.fromkeys(str(b) for b in bits)))
        if loc.get("country") and loc["country"] not in countries:
            countries.append(str(loc["country"]))
    text = "; ".join(parts) or str(o.get("location") or "")
    if o.get("remote") and "remote" not in text.lower():
        text = f"Remote ({text})" if text else "Remote"
    if not countries and o.get("country"):
        countries.append(str(o["country"]))
    return text[:500], countries


def _salary(s) -> str:
    """'100000-120000 EUR/year' from the salary object, or '' when it is empty or zero."""
    if not isinstance(s, dict) or not any(str(s.get(k) or "0").strip("0.") for k in ("min", "max")):
        return ""
    rng = "-".join(str(s[k]) for k in ("min", "max") if s.get(k))
    return " ".join(x for x in (rng, (s.get("currency") or "") + ("/" + s["period"] if s.get("period") else "")) if x)


def _hints(o: dict, countries: list[str]) -> dict[str, str]:
    """The facts the offer states, under HINT_KEYS names."""
    remote = "yes" if o.get("remote") else "hybrid" if o.get("hybrid") else "no" if o.get("on_site") else ""
    etype = str(o.get("employment_type_code") or "")
    edu = str(o.get("education_code") or "")
    return {
        "level": _LEVELS.get(str(o.get("experience_code") or ""), ""),
        "employment_type": next((v for k, v in _TYPES.items() if etype.startswith(k)), etype),
        "remote": remote,
        "country": ", ".join(countries),
        "salary": _salary(o.get("salary")),
        "category": str(o.get("department") or o.get("category_code") or ""),
        "degrees": edu.replace("_", " ") if edu.endswith("degree") else "",
    }


def _description(o: dict) -> str:
    """Description plus requirements as text, hidden text flagged."""
    raw = "\n".join(str(o.get(k) or "") for k in ("description", "requirements"))
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(o: dict, slug: str) -> Listing:
    """One offer as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(o, dict) or o.get("status", "published") != "published":
        raise ValueError("not a published offer")
    location, countries = _location(o)
    oid = str(o.get("id") or "") or None
    return Listing(
        company=str(o.get("company_name") or slug),
        title=str(o.get("title") or ""),
        url=str(o.get("careers_url") or ""),
        location=location,
        posted_at=_day(o.get("published_at")),
        description=_description(o),
        ats="recruitee",
        ats_job_id=oid,
        source_ref=oid,
        hints=_hints(o, countries),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published offer on this company's Recruitee board, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    data = get_json(api_url(slug))
    out, seen = [], set()
    for o in (data.get("offers") if isinstance(data, dict) else None) or []:
        try:
            x = _listing(o, slug)
        except ValueError as e:
            log.debug("skipping recruitee offer on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.recruitee.com link, or None (custom career domains aren't recognised)."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if not host.endswith(".recruitee.com"):
        return None
    label = host[: -len(".recruitee.com")]
    if "." in label or label in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="recruitee",
        fetch=fetch,
        board_of=board_of,
        example="bunq",
        terms="Public Careers Site API (no key), documented by Recruitee for listing a company's jobs; one request "
        "per board.",
        hosts=("recruitee.com",),
    )
)
