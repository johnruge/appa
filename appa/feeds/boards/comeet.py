"""Comeet job boards, read from each company's public hosted careers page (no key).

Comeet's careers API needs a per-company token, so this reads the page candidates see instead:
GET https://www.comeet.com/jobs/{slug}/{company_uid} embeds every open position as a JS literal,
`COMPANY_POSITIONS_DATA = [...]`, which is decoded as JSON. Each position has its uid, title, department,
location (with country and remote flag), employment type, experience level, workplace type (On-site/Hybrid/
Remote), the hosted posting link, last-updated time, company name and custom fields whose "details" hold the
posting text (Description, Requirements) as HTML. One request per board; no pagination.

A board is "{slug}:{company_uid}", e.g. "team8:61.003". Company uids are two hex groups ("61.003", "1F.05A");
a position posted in several places has one uid per location ("E3.D6C-27.40D").
robots.txt only disallows WordPress paths (search, categories, wp-admin), not /jobs/.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_UID = re.compile(r"^[0-9A-Fa-f]{1,8}\.[0-9A-Fa-f]{1,8}$")
_POS_UID = re.compile(
    r"^[0-9A-Fa-f]{1,8}\.[0-9A-Fa-f]{1,8}(?:-[0-9A-Fa-f]{1,8}\.[0-9A-Fa-f]{1,8}){0,3}$"
)  # "-" joins a per-location copy
_HOSTS = ("comeet.com", "comeet.co")
_MARKER = re.compile(r"\bCOMPANY_POSITIONS_DATA\s*=\s*\[")  # the assignment, not the `var` line
_LEVELS = {
    "intern": "intern",
    "internship": "intern",
    "entry-level": "entry",
    "entry level": "entry",
    "junior": "entry",
    "graduate": "new_grad",
    "new grad": "new_grad",
    "mid-level": "mid",
    "middle": "mid",
    "mid": "mid",
    "senior": "senior",
    "lead": "senior",
    "executive": "senior",
    "director": "senior",
}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "on-site": "no", "onsite": "no"}
MAX_DESC = 100_000


def _split(slug: str) -> tuple[str, str]:
    """(company slug, company uid) from a "slug:uid" board id; ValueError unless both parts are safe."""
    parts = slug.split(":") if isinstance(slug, str) else []
    if len(parts) != 2 or not _SLUG.fullmatch(parts[0]) or not _UID.fullmatch(parts[1]):
        raise ValueError(f"bad comeet board: {slug!r}")
    return parts[0].lower(), parts[1].upper()


def page_url(slug: str) -> str:
    """The hosted careers page URL for a board."""
    name, uid = _split(slug)
    return f"https://www.comeet.com/jobs/{quote(name, safe='')}/{quote(uid, safe='')}"


def _positions(page: str) -> list:
    """The COMPANY_POSITIONS_DATA list embedded in a careers page; ValueError when it's missing."""
    m = _MARKER.search(page)
    if not m:
        raise ValueError("no COMPANY_POSITIONS_DATA on the comeet page")
    data, _ = json.JSONDecoder().raw_decode(page, m.end() - 1)
    return data if isinstance(data, list) else []


def _day(v) -> date | None:
    """'2026-10-31T08:49:19Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _description(p: dict) -> str:
    """The posting text from the custom "details" fields (Description, Requirements...), hidden text flagged."""
    cf = p.get("custom_fields") if isinstance(p.get("custom_fields"), dict) else {}
    details = [d for d in (cf.get("details") or []) if isinstance(d, dict) and isinstance(d.get("value"), str)]
    parts, hidden = [], []
    for d in sorted(details, key=lambda d: d.get("order") if isinstance(d.get("order"), int) else 0):
        text, h = html_to_text(d["value"])
        if text:
            parts.append(f"{d.get('name')}\n{text}" if d.get("name") else text)
        hidden += h
    return mark_hidden("\n\n".join(parts), hidden)[:MAX_DESC]


def _etype(v) -> str:
    """'Full-time' / 'full time' -> 'full_time'."""
    return "_".join(re.findall(r"[a-z]+", str(v or "").lower()))


def _listing(p: dict, slug: str, company_uid: str) -> Listing:
    """One position as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(p, dict):
        raise ValueError("not a position")
    uid = str(p.get("uid") or "")
    if not _POS_UID.fullmatch(uid):
        raise ValueError(f"bad position uid: {uid[:40]!r}")
    loc = p.get("location") if isinstance(p.get("location"), dict) else {}
    remote = _REMOTE.get(str(p.get("workplace_type") or "").lower(), "yes" if loc.get("is_remote") else "")
    location = str(loc.get("name") or "")
    if remote == "yes" and "remote" not in location.lower():
        location = f"Remote ({location})" if location else "Remote"
    active = str(p.get("url_active_page") or "")
    hints = {
        "level": _LEVELS.get(str(p.get("experience_level") or "").lower(), ""),
        "remote": remote,
        "country": str(loc.get("country") or ""),
        "employment_type": _etype(p.get("employment_type")),
        "category": str(p.get("department") or ""),
        "links": active if active.startswith(("http://", "https://")) else "",
    }
    return Listing(
        company=str(p.get("company_name") or slug),
        title=str(p.get("name") or ""),
        url=str(p.get("url_comeet_hosted_page") or ""),
        location=location[:500],
        posted_at=_day(p.get("time_updated")),
        description=_description(p),
        ats="comeet",
        ats_job_id=f"{company_uid}:{uid}",
        source_ref=uid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open public position on this company's Comeet careers page, with descriptions (one request)."""
    opts = opts or FeedOptions()
    name, company_uid = _split(slug)
    page, final = get_text(page_url(slug))
    if not urlparse(final).path.lower().startswith("/jobs/"):
        return []  # a closed board redirects to comeet.com's home page
    out, seen = [], set()
    for p in _positions(page):
        if isinstance(p, dict) and p.get("is_internal"):
            continue
        try:
            x = _listing(p, name, company_uid)
        except ValueError as e:
            log.debug("skipping comeet position on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The "slug:uid" board of a comeet.com/jobs/{slug}/{uid}[/...] link (or comeet.co), else None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in _HOSTS):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 3 or parts[0].lower() != "jobs":
        return None
    try:
        name, uid = _split(f"{parts[1]}:{parts[2]}")
    except ValueError:
        return None
    return f"{name}:{uid}"


BOARD = register_board(
    BoardFeed(
        ats="comeet",
        fetch=fetch,
        board_of=board_of,
        example="team8:61.003",
        terms="Public hosted careers pages (no key; robots.txt only disallows WordPress paths); one request per "
        "board, descriptions included.",
        hosts=("www.comeet.com",),
    )
)
