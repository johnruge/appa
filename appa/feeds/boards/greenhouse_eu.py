"""Greenhouse boards, US and EU hosted (job-boards[.eu].greenhouse.io/{slug}), with posting dates.

EU boards are served by the same public API as US ones (boards-api.greenhouse.io/v1/boards/{slug}/jobs;
there is no boards-api.eu host, no key, one response holds the whole board), and their postings link back to
job-boards.eu.greenhouse.io. So this is not a new source: it is a stricter replacement for the "greenhouse"
board in appa/feeds/boards/greenhouse.py, registered under the same "greenhouse" key so it supersedes that
module's fetch and board_of (load_all imports greenhouse first, then this). What it adds:

- fetch adds posted_at (first_published), which the shared appa/sources/ats parser drops. Postings and
  descriptions still come from greenhouse.fetch (ats.fetch_board, which the pipeline tests stub); the dates
  come from one extra, light request to the same endpoint without content (about 5% of the full size),
  best effort. opts.since and opts.max_results apply.
- board_of matches exact Greenhouse hosts only (no substrings), knows the EU hosts and embed links
  (`/embed/job_board?for={slug}`, which the shared parser reads as the slug "embed"), and ignores
  non-board paths (static, assets, embed...).

Pending owner-approved shared changes (reported as interface requests), this override should move into
appa/sources/ats._gh_job + appa/core/ats_urls.py and this module be deleted. Nothing imports
greenhouse.BOARD directly; every caller goes through feeds.boards_registry(), which runs load_all.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from urllib.parse import parse_qs, quote, urlsplit

from appa.feeds import FeedOptions, Listing, register_board
from appa.feeds.boards import greenhouse
from appa.sources.http import get_json

API = "https://boards-api.greenhouse.io/v1/boards/"
HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
EMBEDS = {"job_board", "job_app"}  # /embed/job_board?for=slug and /embed/job_app?for=slug&token=id
NOT_BOARDS = {"embed", "static", "assets", "favicon.ico", "robots.txt", "sitemap.xml", "api", "v1"}
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


def check_slug(slug: str) -> str:
    """The slug if it is a plain Greenhouse board token; ValueError otherwise."""
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or slug.lower() in NOT_BOARDS:
        raise ValueError(f"not a Greenhouse board slug: {slug!r}"[:200])
    return slug


def _day(stamp) -> date | None:
    """The date part of a Greenhouse ISO timestamp, or None."""
    try:
        return date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None


def _dates(slug: str) -> dict[str, date]:
    """{job id: first published date} from the board's light listing (no content); {} if it can't be read."""
    try:
        data = get_json(API + quote(slug, safe="") + "/jobs")
        rows = (data.get("jobs") or []) if isinstance(data, dict) else []
    except Exception:  # best effort: a board without dates is still a board
        return {}
    out: dict[str, date] = {}
    for j in rows:
        if isinstance(j, dict) and j.get("id") is not None:
            if day := _day(j.get("first_published")) or _day(j.get("updated_at")):
                out[str(j["id"])] = day
    return out


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on a Greenhouse board (US or EU hosted), with descriptions and posting dates."""
    opts = opts or FeedOptions()
    rows = greenhouse.fetch(check_slug(slug), opts)
    dates = _dates(slug) if rows else {}
    out: list[Listing] = []
    for x in rows:
        day = dates.get(x.ats_job_id or "")
        if opts.since and day and day < opts.since:
            continue
        out.append(x.model_copy(update={"posted_at": day}))
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The Greenhouse board slug in a US or EU Greenhouse link (board, posting or embed), else None."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except (ValueError, AttributeError):
        return None
    if parts.scheme.lower() not in ("http", "https") or host not in HOSTS:
        return None
    path = [p for p in parts.path.split("/") if p]
    if not path:
        return None
    if path[0].lower() == "embed":
        slug = parse_qs(parts.query).get("for", [""])[0] if len(path) > 1 and path[1] in EMBEDS else ""
    elif len(path) == 1 or (len(path) >= 2 and path[1] == "jobs"):
        slug = path[0]  # /{slug}, /{slug}/jobs, /{slug}/jobs/{id}
    else:
        return None
    try:
        return check_slug(slug)
    except ValueError:
        return None


BOARD = register_board(
    replace(
        greenhouse.BOARD,
        fetch=fetch,
        board_of=board_of,
        terms="Public Greenhouse Job Board API; no key. One request per board.",
    )
)
