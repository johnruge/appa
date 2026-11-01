"""EU-region Lever job boards, through Lever's public postings API on its EU host (no key).

Lever hosts EU customers separately: their boards live at jobs.eu.lever.co/{slug} and the US API
(api.lever.co) returns [] for them. GET https://api.eu.lever.co/v0/postings/{slug}?mode=json returns
the same schema as the US API: id, text, categories (location, team, department, commitment),
hostedUrl, applyUrl, createdAt (ms), workplaceType, country, salaryRange and the description as HTML
and plain text. It pages with skip/limit. Lever documents this API for public job listings
(github.com/lever/postings-api); robots.txt allows all with Crawl-delay: 1, so pages are 1s apart.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://api.eu.lever.co/v0/postings/"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NOT_BOARDS = {"robots.txt", "favicon.ico", "sitemap.xml"}
_TYPES = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "internship": "internship",
    "intern": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "fixed term": "contract",
}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no", "on-site": "no"}
PAGE = 100
MAX_PAGES = 50
DELAY = 1.0
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Lever board name; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or ".." in slug:
        raise ValueError(f"bad lever_eu slug: {slug!r}")
    return slug


def api_url(slug: str) -> str:
    """The EU postings API URL for a board."""
    return API + quote(_check(slug), safe="")


def _day(ms) -> date | None:
    """Lever's createdAt (epoch milliseconds) -> date, or None."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _salary(s) -> str:
    """'148000-185000 USD per-year-salary' from salaryRange, or ''."""
    if not isinstance(s, dict) or not (s.get("min") or s.get("max")):
        return ""
    rng = "-".join(str(s[k]) for k in ("min", "max") if s.get(k))
    return " ".join(str(x) for x in (rng, s.get("currency"), s.get("interval")) if x)


def _hints(j: dict) -> dict[str, str]:
    """The facts the posting states, under HINT_KEYS names."""
    cat = j.get("categories") if isinstance(j.get("categories"), dict) else {}
    commitment = str(cat.get("commitment") or "")
    return {
        "employment_type": _TYPES.get(commitment.lower().strip(), ""),
        "level": "intern" if "intern" in commitment.lower() else "",
        "remote": _REMOTE.get(str(j.get("workplaceType") or "").lower(), ""),
        "country": str(j.get("country") or ""),
        "salary": _salary(j.get("salaryRange")),
        "category": str(cat.get("department") or cat.get("team") or ""),
    }


def _description(j: dict) -> str:
    """Description, lists, salary note and closing text as plain text, hidden text flagged (as in
    appa/sources/ats.py)."""
    parts = [str(j.get("descriptionPlain") or "")]
    for lst in j.get("lists") or []:
        if isinstance(lst, dict):
            parts.append(f"{lst.get('text') or ''}\n{html_to_text(str(lst.get('content') or ''))[0]}")
    parts += [str(j.get(k) or "") for k in ("salaryDescriptionPlain", "additionalPlain")]
    hidden: list[str] = []
    for k in ("description", "additional", "salaryDescription"):
        hidden += html_to_text(str(j.get(k) or ""))[1]
    for lst in j.get("lists") or []:
        if isinstance(lst, dict):
            hidden += html_to_text(str(lst.get("content") or ""))[1]
    return mark_hidden("\n\n".join(p for p in parts if p.strip()), hidden)[:MAX_DESC]


def _location(j: dict) -> str:
    """All of the posting's locations, joined, else its primary location."""
    cat = j.get("categories") if isinstance(j.get("categories"), dict) else {}
    every = [str(x) for x in cat.get("allLocations") or [] if x]
    return "; ".join(every) or str(cat.get("location") or "")


def _listing(j: dict, slug: str) -> Listing:
    """One posting as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict) or not j.get("id"):
        raise ValueError("not a posting")
    url = str(j.get("hostedUrl") or "")
    if (urlparse(url).hostname or "").lower() != "jobs.eu.lever.co":
        raise ValueError(f"unexpected posting link {url[:100]!r}")
    jid = str(j["id"])
    return Listing(
        company=slug,
        title=str(j.get("text") or ""),
        url=url,
        location=_location(j)[:500],
        posted_at=_day(j.get("createdAt")),
        description=_description(j),
        ats="lever_eu",
        ats_job_id=jid,
        source_ref=jid,
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's EU Lever board, with descriptions, paged 100 at a time."""
    opts = opts or FeedOptions()
    url = api_url(slug)
    out, seen = [], set()
    for page in range(MAX_PAGES):
        if page:
            time.sleep(DELAY)
        rows = get_json(url, {"mode": "json", "skip": page * PAGE, "limit": PAGE})
        if not isinstance(rows, list) or not rows:
            break
        for j in rows:
            try:
                x = _listing(j, slug)
            except ValueError as e:
                log.debug("skipping lever_eu posting on %s: %s", slug, e)
                continue
            if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        if len(rows) < PAGE:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a jobs.eu.lever.co/{slug} (or api.eu.lever.co/v0/postings/{slug}) link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    parts = [x for x in p.path.split("/") if x]
    if host == "jobs.eu.lever.co" and parts:
        slug = parts[0]
    elif host == "api.eu.lever.co" and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        slug = parts[2]
    else:
        return None
    if slug.lower() in _NOT_BOARDS:
        return None
    try:
        return _check(slug)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="lever_eu",
        fetch=fetch,
        board_of=board_of,
        example="quantinuum",
        terms="Lever's public postings API (no key) on its EU host; robots.txt asks Crawl-delay: 1, so pages are 1s "
        "apart.",
        hosts=("api.eu.lever.co",),
    )
)
