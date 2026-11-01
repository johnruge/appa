"""Hireology job boards, through the public careers-site JSON API (no key).

GET https://api.hireology.com/v2/public/careers/{slug}?page=N&page_size=100 returns
{data: [...], count, page, page_size}: the open jobs on a company's careers.hireology.com/{slug} page, each
with its description HTML, locations, remote flag, employment status, job family, organization name, posted
pay and the human posting link (career_site_url). It is the API Hireology's own careers pages read, and
careers.hireology.com/robots.txt allows all agents. Evergreen "Join our Talent Community" rows are skipped.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://api.hireology.com/v2/public/careers/"
SITE_HOST = "careers.hireology.com"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NOT_BOARDS = {"sitemaps", "sitemap_index.xml", "robots.txt", "assets", "packs", "careers", "api", "favicon.ico"}
_TALENT = re.compile(r"talent\s+(community|network|pool)", re.I)
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_DESC = 100_000
PAUSE = 0.5


def _check(slug: str) -> str:
    """The slug, if it is a safe careers.hireology.com path segment; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or set(slug) <= {"."}:
        raise ValueError(f"bad hireology slug: {slug!r}")
    return slug


def api_url(slug: str) -> str:
    """The careers API URL for a board."""
    return API + quote(_check(slug), safe="")


def _day(v) -> date | None:
    """'2026-10-05T20:49:37.688Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _num(v) -> float:
    """A '145000.0' amount as a float (0 when missing or bad)."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _salary(c) -> str:
    """'130000-150000/year' or '145000/year' from the compensation object, or '' when no pay is posted."""
    if not isinstance(c, dict):
        return ""
    lo, hi, one = _num(c.get("comp_range_min")), _num(c.get("comp_range_max")), _num(c.get("comp_single_amount"))
    if c.get("is_comp_range") and (lo or hi):
        amount = "-".join(f"{x:g}" for x in (lo, hi) if x)
    elif one:
        amount = f"{one:g}"
    else:
        return ""
    period = str(c.get("comp_period") or "")
    return f"{amount}/{period}" if period else amount


def _employment(v: str) -> str:
    """'Full Time - salary' -> 'full_time' (other labels kept verbatim)."""
    low = v.lower()
    for key, out in (
        ("intern", "internship"),
        ("full time", "full_time"),
        ("part time", "part_time"),
        ("contract", "contract"),
        ("temporary", "temporary"),
        ("seasonal", "temporary"),
    ):
        if key in low:
            return out
    return v


def _location(j: dict) -> str:
    """'Chicago, IL; Remote' from the job's locations and remote flag."""
    parts = []
    for loc in j.get("locations") or []:
        if isinstance(loc, dict):
            bits = [str(loc[k]) for k in ("city", "state") if loc.get(k)]
            if bits:
                parts.append(", ".join(bits))
    text = "; ".join(dict.fromkeys(parts))
    if j.get("remote") and "remote" not in text.lower():
        text = f"{text}; Remote" if text else "Remote"
    return text[:500]


def _hints(j: dict) -> dict[str, str]:
    """The facts the job states, under HINT_KEYS names."""
    family = j.get("job_family") if isinstance(j.get("job_family"), dict) else {}
    return {
        "remote": "yes" if j.get("remote") is True else "",
        "employment_type": _employment(str(j.get("employment_status") or "")),
        "salary": _salary(j.get("compensation")),
        "category": str(family.get("name") or ""),
    }


def _description(j: dict) -> str:
    """The description HTML as text, hidden text flagged."""
    text, hidden = html_to_text(str(j.get("job_description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(j: dict, slug: str) -> Listing:
    """One job as a Listing (ValueError for closed, evergreen or malformed rows)."""
    if not isinstance(j, dict) or str(j.get("status") or "Open").lower() != "open":
        raise ValueError("not an open job")
    title = str(j.get("name") or "")
    if _TALENT.search(title):
        raise ValueError("talent community row")
    jid = str(j.get("id") or "")
    if not jid.isdigit():
        raise ValueError("missing job id")
    url = str(j.get("career_site_url") or "")
    if not board_of(url):
        url = f"https://{SITE_HOST}/{quote(slug, safe='')}/{jid}/description"
    org = j.get("organization") if isinstance(j.get("organization"), dict) else {}
    return Listing(
        company=str(org.get("name") or slug),
        title=title,
        url=url,
        location=_location(j),
        posted_at=_day(j.get("created_at")),
        description=_description(j),
        ats="hireology",
        ats_job_id=jid,
        source_ref=jid,
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this company's Hireology board, with descriptions (paged, 100 per page)."""
    opts = opts or FeedOptions()
    url = api_url(slug)
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        data = get_json(url, params={"page": page, "page_size": PAGE_SIZE})
        rows = (data.get("data") if isinstance(data, dict) else None) or []
        for j in rows:
            try:
                x = _listing(j, slug)
            except ValueError as e:
                log.debug("skipping hireology job on %s: %s", slug, e)
                continue
            if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        try:
            count, size = int(data.get("count") or 0), int(data.get("page_size") or PAGE_SIZE)
        except (TypeError, ValueError):
            break
        if not rows or page * max(size, 1) >= count:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a careers.hireology.com/{slug}[/{id}/description] link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if host != SITE_HOST or p.scheme not in ("http", "https"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts or parts[0].lower() in _NOT_BOARDS:
        return None
    if len(parts) > 1 and not (parts[1].isdigit() and parts[2:] in ([], ["description"], ["application"])):
        return None
    try:
        return _check(parts[0])
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="hireology",
        fetch=fetch,
        board_of=board_of,
        example="hireology2",
        terms="Public careers-site JSON API (no key) that Hireology's own careers pages use; robots.txt allows all; "
        "paged 100 per request with a short pause.",
        hosts=("api.hireology.com",),
    )
)
