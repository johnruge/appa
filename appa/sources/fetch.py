"""Any job link -> Job, through the best source for it:

1. Greenhouse/Lever/Ashby links: that ATS's posting API (the fast path in sources/ats.py);
2. links on any other ATS appa has a board adapter for (Workday, SmartRecruiters, Workable, iCIMS, ... see
   appa/feeds/boards): the posting is looked up on its company's board, with the adapter's full text;
3. anything else: the page itself (which may embed an ATS).

All requests go through the guarded HTTP module."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from appa.core.models import Job
from appa.sources import ats, generic

log = logging.getLogger(__name__)

BOARD_CACHE_S = 600  # one board fetch serves every link to that board for 10 minutes
_boards: dict[tuple[str, str], tuple[float, list]] = {}


def fetch_posting(url: str) -> Job:
    """Fetch a posting through its ATS: the posting API for the big three, a board adapter for the rest, else
    the page."""
    ref = ats.parse_url(url)
    if ref and ref.slug and ref.job_id:
        return ats.fetch_job(ref)
    if job := _via_board(url):
        return job
    return generic.fetch(url)


def _norm(url: str) -> str:
    """A link without scheme, www, query, fragment or trailing slash, for matching a posting to its board entry."""
    u = urlparse(url.strip())
    return f"{(u.hostname or '').lower().removeprefix('www.')}{u.path.rstrip('/')}".lower()


def _options():
    """The same search options sweeps use (your policy's queries), so a link found by a sweep is found again."""
    from appa.feeds import FeedOptions

    try:
        from appa.agents import screener

        f = screener.load_policy().feeds
        return FeedOptions(queries=tuple(f.queries), us_only=f.us_only)
    except Exception:  # no policy available (e.g. a bare script): the defaults
        return FeedOptions()


def _board_listings(ats_name: str, slug: str, board) -> list:
    """A board's open postings, from a short-lived cache when the same board was fetched moments ago."""
    hit = _boards.get((ats_name, slug))
    if hit and time.monotonic() - hit[0] < BOARD_CACHE_S:
        return hit[1]
    listed = board.fetch(slug, _options())
    _boards[(ats_name, slug)] = (time.monotonic(), listed)
    if len(_boards) > 200:  # keep the cache small
        _boards.pop(next(iter(_boards)))
    return listed


def _via_board(url: str) -> Job | None:
    """The posting from its company's board on any ATS appa has an adapter for, or None (then the page is
    fetched)."""
    from appa import feeds

    found = feeds.board_of(url)
    if not found:
        return None
    ats_name, slug = found
    board = feeds.boards_registry()[ats_name]
    try:
        listed = _board_listings(ats_name, slug, board)
    except Exception as e:  # the board failed: the page may still work
        log.info("board %s:%s failed for %s: %s", ats_name, slug, url, e)
        return None
    want = _norm(url)
    path = urlparse(url).path
    match = next((x for x in listed if _norm(x.url) == want), None) or next(
        (x for x in listed if x.ats_job_id and len(x.ats_job_id) >= 4 and x.ats_job_id in path), None
    )
    if not match:
        return None  # closed, or outside what the board lists
    if not match.description and board.describe:
        try:
            match = match.model_copy(update={"description": board.describe(match) or ""})
        except Exception as e:
            log.info("describe failed for %s: %s", url, e)
    job = match.to_job("link")
    if not job.description:  # the page has the text the board didn't
        try:
            job.description = generic.fetch(url).description
        except Exception:
            pass
    job.url = url
    return job
