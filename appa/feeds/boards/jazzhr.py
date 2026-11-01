"""JazzHR job boards, through the public XML job export feed (no key).

GET https://app.jazz.co/feeds/export/jobs/{slug} returns every open job on a company's JazzHR career page
({slug}.applytojob.com) in one response: <company>, then one <job> per posting with <id>
(job_YYYYMMDDhhmmss_XXXX, the created time is in the id), <title>, <department>, <url> (the
applytojob.com apply page), <city>/<state>/<country>, <type>, <experience> and <description> (HTML in
CDATA). JazzHR publishes this feed for job-board syndication; there is no paging and no login. An unknown
slug is a 404 with an empty body. The {slug}.applytojob.com/apply/jobs/feed URL only redirects, so the
app.jazz.co URL is used. Poll about daily per board.
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

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under applytojob.com
_ID = re.compile(r"^job_(\d{8})\d{6}_[A-Za-z0-9]{1,40}$")
_CODE = re.compile(r"^/apply/([A-Za-z0-9]{1,40})(?:/|$)")
_NOT_BOARDS = {"www", "app", "api", "static", "assets", "cdn"}
_TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "contractor": "contract",
    "contract": "contract",
    "temporary": "contract",
    "seasonal": "contract",
    "intern": "internship",
    "internship": "internship",
    "per diem": "per_diem",
    "volunteer": "volunteer",
}
_LEVELS = {
    "entry level": "entry",
    "mid level": "mid",
    "student (college)": "intern",
    "internship": "intern",
    "student (high school)": "intern",
}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe applytojob.com subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad jazzhr slug: {slug!r}")
    return slug.lower()


def feed_url(slug: str) -> str:
    """The XML job export feed URL for a board."""
    return f"https://app.jazz.co/feeds/export/jobs/{quote(_check(slug), safe='')}"


def _text(el, tag: str) -> str:
    """Stripped text of a child element, or ''."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _day(jid: str) -> date | None:
    """The created date in a 'job_20260911192107_...' id, or None."""
    m = _ID.fullmatch(jid)
    try:
        return date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:])) if m else None
    except ValueError:
        return None


def _apply_url(raw: str, slug: str) -> tuple[str, str | None]:
    """(apply page URL, its posting code): the feed's URL when it is on this board, else ValueError."""
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() != f"{slug}.applytojob.com":
        raise ValueError(f"jazzhr job url off the board: {raw[:80]!r}")
    m = _CODE.match(p.path)
    return raw, (m[1] if m else None)


def _hints(j, location: str) -> dict[str, str]:
    """The facts the job states, under HINT_KEYS names."""
    etype, level = _text(j, "type").lower(), _text(j, "experience").lower()
    return {
        "employment_type": _TYPES.get(etype, ""),
        "level": _LEVELS.get(level, ""),
        "category": _text(j, "department"),
        "country": _text(j, "country"),
        "remote": "yes" if "remote" in location.lower() else "",
    }


def _description(j) -> str:
    """The job's HTML description as text, hidden text flagged."""
    text, hidden = html_to_text(_text(j, "description"))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(j, slug: str, company: str) -> Listing:
    """One <job> as a Listing (ValueError when a required field is missing or bad)."""
    jid = _text(j, "id")
    if not _ID.fullmatch(jid) or len(jid) > 100:
        raise ValueError(f"bad jazzhr job id: {jid[:30]!r}")
    url, code = _apply_url(_text(j, "url"), slug)
    location = ", ".join(x for x in (_text(j, "city"), _text(j, "state"), _text(j, "country")) if x)
    return Listing(
        company=company or slug,
        title=_text(j, "title"),
        url=url,
        location=location[:500],
        posted_at=_day(jid),
        description=_description(j),
        ats="jazzhr",
        ats_job_id=code or jid,
        source_ref=jid,
        hints=_hints(j, location),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this company's JazzHR board, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    root = ElementTree.fromstring(get_bytes(feed_url(slug)))
    company = _text(root, "company")
    out, seen = [], set()
    for j in root.findall("job"):
        status = _text(j, "status")
        if status and status.lower() != "open":
            continue
        try:
            x = _listing(j, slug, company)
        except ValueError as e:
            log.debug("skipping jazzhr job on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.source_ref in seen:
            continue
        seen.add(x.source_ref)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.applytojob.com or app.jazz.co/feeds/export/jobs/{slug} link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if host == "app.jazz.co":
        parts = p.path.split("/")
        label = parts[4] if len(parts) >= 5 and parts[1:4] == ["feeds", "export", "jobs"] else ""
    elif host.endswith(".applytojob.com"):
        label = host[: -len(".applytojob.com")]
    else:
        return None
    if not label or "." in label or label.lower() in _NOT_BOARDS:
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="jazzhr",
        fetch=fetch,
        board_of=board_of,
        example="snapsheet",
        terms="Public XML job export feed (no key) that JazzHR publishes for job-board syndication; "
        "one request per board, about daily.",
        hosts=("app.jazz.co",),
    )
)
