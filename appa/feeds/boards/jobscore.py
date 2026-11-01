"""JobScore job boards, through each company's public careers JSON feed (no key).

GET https://careers.jobscore.com/jobs/{slug}/feed.json returns every open job on a company's JobScore
careers site in one response (no pagination): id, title, the posting page, department, location and
country, a remote label ("Yes | ...", "Hybrid | ...", "No | ..."), job type, experience level, the posted
pay range when the employer shows one, opened date and the full HTML description. It's the feed JobScore
publishes for job distribution; robots.txt only disallows /apply_flow/. The slug is the company code in
careers.jobscore.com/careers/{slug} (lowercase; the feed 404s on other casings).
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

BASE = "https://careers.jobscore.com"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_PART = re.compile(r"^[A-Za-z0-9_-]{1,300}$")  # job ids and url slugs
_HOSTS = ("careers.jobscore.com", "widgets.jobscore.com", "www.jobscore.com", "jobscore.com")
_TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "internship": "internship",
    "intern": "internship",
    "seasonal": "temporary",
    "per diem": "part_time",
    "volunteer": "volunteer",
}
_LEVELS = {
    "internship": "intern",
    "entry level": "entry",
    "associate": "entry",
    "mid-level": "mid",
    "experienced (non-manager)": "senior",
    "manager (manager/supervisor of staff)": "senior",
    "executive": "senior",
    "director": "senior",
}
_REMOTE = {"yes": "yes", "hybrid": "hybrid", "no": "no"}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug lowercased, if it is a safe JobScore company code; else ValueError."""
    s = slug.strip().lower() if isinstance(slug, str) else ""
    if not _SLUG.fullmatch(s):
        raise ValueError(f"bad jobscore slug: {slug!r}")
    return s


def api_url(slug: str) -> str:
    """The careers JSON feed URL for a board."""
    return f"{BASE}/jobs/{quote(_check(slug), safe='')}/feed.json"


def _day(v) -> date | None:
    """'2026-08-27T20:53:06.422Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _posting_url(j: dict, slug: str, jid: str) -> str:
    """The human posting page, rebuilt from validated parts (the feed's link minus its tracking query)."""
    part = str(j.get("url_slug") or "")
    return f"{BASE}/careers/{quote(slug, safe='')}/jobs/{quote(part if _PART.fullmatch(part) else jid, safe='')}"


def _description(raw) -> str:
    """The posting's HTML as text, hidden text flagged."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _hints(j: dict) -> dict[str, str]:
    """Facts the feed states: employment type, level, remote, country, salary, department."""
    etype = str(j.get("job_type") or "").strip()
    level = str(j.get("experience_level") or "").strip()
    remote = str(j.get("remote") or "").split("|")[0].strip().lower()
    return {
        "employment_type": _TYPES.get(etype.lower(), etype),
        "level": _LEVELS.get(level.lower(), ""),
        "remote": _REMOTE.get(remote, ""),
        "country": str(j.get("country") or ""),
        "salary": str(j.get("formatted_public_compensation") or ""),
        "category": str(j.get("department") or ""),
    }


def _listing(j: dict, slug: str, company: str) -> Listing:
    """One job as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict):
        raise ValueError("not a job")
    jid = str(j.get("id") or "")
    if not _PART.fullmatch(jid) or len(jid) > 100:
        raise ValueError(f"bad job id: {jid[:50]!r}")
    return Listing(
        company=str(j.get("company_name") or company or slug),
        title=str(j.get("title") or ""),
        url=_posting_url(j, slug, jid),
        location=str(j.get("location") or "")[:500],
        posted_at=_day(j.get("opened_date") or j.get("created_on")),
        description=_description(j.get("description")),
        ats="jobscore",
        ats_job_id=jid,
        source_ref=jid,
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this company's JobScore board, with descriptions (one request)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    data = get_json(api_url(slug))
    data = data if isinstance(data, dict) else {}
    company = str(data.get("company_name") or data.get("company") or "")
    out, seen = [], set()
    for j in data.get("jobs") if isinstance(data.get("jobs"), list) else []:
        try:
            x = _listing(j, slug, company)
        except ValueError as e:
            log.debug("skipping jobscore job on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The company code in a JobScore careers/jobs/widget link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host not in _HOSTS:
        return None
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) < 2 or parts[0] not in ("careers", "jobs"):
        return None
    try:
        return _check(parts[1])
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="jobscore",
        fetch=fetch,
        board_of=board_of,
        example="matrixspace",
        terms="Public careers JSON feed JobScore publishes for job distribution (no key; robots.txt allows it); "
        "one request per board.",
        hosts=("careers.jobscore.com",),
    )
)
