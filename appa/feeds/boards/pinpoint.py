"""Pinpoint job boards, through the public postings JSON feed (no key).

GET https://{slug}.pinpointhq.com/postings.json returns every posting on a company's career site in one
response ({"data": [...]}, no paging): title, the human posting link, the description, responsibilities,
skills and benefits HTML, compensation (only when the employer shows it), employment and workplace type,
location and department. Pinpoint documents this endpoint for listing a company's jobs on other sites
(developers.pinpointhq.com, "Job Postings JSON Endpoint"). The feed has no company name or posting date,
so the slug stands in for the company and posted_at stays empty. An unknown slug is a 404 HTML page.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under pinpointhq.com
_UUID = re.compile(r"/postings/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "developers",
    "docs",
    "help",
    "support",
    "blog",
    "status",
    "cdn",
    "static",
    "assets",
    "mail",
    "auth",
    "login",
    "id",
    "marketplace",
    "partners",
    "pinpoint-demo",
}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no"}
_SECTIONS = (
    ("description", ""),
    ("key_responsibilities", "Key Responsibilities"),
    ("skills_knowledge_expertise", "Skills, Knowledge and Expertise"),
    ("benefits", "Benefits"),
)
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Pinpoint subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad pinpoint slug: {slug!r}")
    return slug.lower()


def api_url(slug: str) -> str:
    """The postings JSON feed URL for a board."""
    return f"https://{quote(_check(slug), safe='')}.pinpointhq.com/postings.json"


def _location(loc) -> str:
    """'Minneapolis, MN' from the location object, adding the city and province when the name lacks them."""
    if not isinstance(loc, dict):
        return ""
    name = str(loc.get("name") or "").strip()
    extra = [
        str(loc.get(k)).strip()
        for k in ("city", "province")
        if loc.get(k) and str(loc.get(k)).strip().lower() not in name.lower()
    ]
    return ", ".join(x for x in [name, *extra] if x)[:500]


def _salary(p: dict) -> str:
    """The posted pay text ('$96,900 - $124,266.60 / year') when the employer shows it, else ''."""
    if not p.get("compensation_visible"):
        return ""
    text = str(p.get("compensation") or "").strip()
    if text:
        return text
    rng = "-".join(str(p[k]) for k in ("compensation_minimum", "compensation_maximum") if p.get(k))
    unit = " ".join(str(p[k]) for k in ("compensation_currency", "compensation_frequency") if p.get(k))
    return " ".join(x for x in (rng, unit) if x) if rng else ""


def _hints(p: dict) -> dict[str, str]:
    """The facts the posting states, under HINT_KEYS names."""
    job = p.get("job") if isinstance(p.get("job"), dict) else {}
    dept = job.get("department") if isinstance(job.get("department"), dict) else {}
    return {
        "remote": _REMOTE.get(str(p.get("workplace_type") or ""), ""),
        "employment_type": str(p.get("employment_type") or ""),
        "salary": _salary(p),
        "category": str(dept.get("name") or ""),
    }


def _description(p: dict) -> str:
    """Description, responsibilities, skills and benefits as text, hidden text flagged."""
    parts = []
    for key, default in _SECTIONS:
        if p.get(key):
            head = str(p.get(f"{key}_header") or default)
            parts.append((f"<h3>{head}</h3>" if head else "") + str(p[key]))
    text, hidden = html_to_text("\n".join(parts))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(p: dict, slug: str) -> Listing:
    """One posting as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(p, dict):
        raise ValueError("not a posting")
    url = str(p.get("url") or "")
    m = _UUID.search(urlparse(url).path)
    pid = str(p.get("id") or "") or None
    return Listing(
        company=slug,
        title=str(p.get("title") or ""),
        url=url,
        location=_location(p.get("location")),
        description=_description(p),
        ats="pinpoint",
        ats_job_id=m.group(1).lower() if m else pid,
        source_ref=pid,
        hints=_hints(p),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every posting on this company's Pinpoint board, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    try:
        data = get_json(api_url(slug))
    except json.JSONDecodeError:  # an HTML page instead of the feed: not a Pinpoint board
        log.info("pinpoint board %s returned no JSON feed", slug)
        return []
    out, seen = [], set()
    for p in (data.get("data") if isinstance(data, dict) else None) or []:
        try:
            x = _listing(p, slug)
        except ValueError as e:
            log.debug("skipping pinpoint posting on %s: %s", slug, e)
            continue
        if (x.ats_job_id or x.url) in seen:
            continue
        seen.add(x.ats_job_id or x.url)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.pinpointhq.com link, or None (custom career domains aren't recognised)."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if not host.endswith(".pinpointhq.com"):
        return None
    label = host[: -len(".pinpointhq.com")]
    if "." in label or label in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="pinpoint",
        fetch=fetch,
        board_of=board_of,
        example="coforma",
        terms="Public postings.json feed (no key), documented by Pinpoint for listing a company's jobs elsewhere; "
        "one request per board. robots.txt disallows only /mydata, /admin and /companies.",
        hosts=("pinpointhq.com",),
    )
)
