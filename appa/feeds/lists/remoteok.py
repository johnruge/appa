"""Remote OK: a large remote-only job board (tech, plus sales, ops, support...). Its public API is one
unauthenticated GET that returns the ~100 newest postings with full HTML descriptions; there is no
pagination or search (query params redirect to the HTML homepage), so appa polls it often instead.

The first element of the reply is a legal notice: the API terms ask that users link back to the
posting on Remote OK and name Remote OK as the source, which appa does by keeping the remoteok.com
posting URL as the listing's link. Some text arrives double-encoded (UTF-8 read as Latin-1); it is
repaired here.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://remoteok.com/api"
MAX_DESCRIPTION = 100_000


def _fix(s) -> str:
    """Undo Remote OK's occasional double UTF-8 encoding ("MÃ©xico" -> "México"); other text unchanged."""
    s = str(s or "")
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _date(row: dict) -> datetime | None:
    """When the posting went up, from its unix epoch (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(row.get("epoch")), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _salary(row: dict) -> str:
    """The posted yearly USD range ("$80,000 - $100,000"), or "" when Remote OK shows none (0/0)."""
    try:
        lo, hi = int(row.get("salary_min") or 0), int(row.get("salary_max") or 0)
    except (TypeError, ValueError):
        return ""
    if lo <= 0 and hi <= 0:
        return ""
    if lo <= 0 or hi <= 0 or lo == hi:
        return f"${max(lo, hi):,}"
    return f"${lo:,} - ${hi:,}"


def _listing(row: dict) -> Listing | None:
    """A Listing from one API row, or None for the legal notice and malformed rows."""
    if not isinstance(row, dict) or not row.get("id"):
        return None
    url = str(row.get("url") or row.get("apply_url") or "")
    text, hidden = html_to_text(_fix(row.get("description")))
    posted = _date(row)
    hints = {"remote": "yes", "salary": _salary(row)}
    try:
        return Listing(
            company=html.unescape(_fix(row.get("company"))),
            title=html.unescape(_fix(row.get("position"))),
            url=url,
            location=_fix(row.get("location"))[:500],
            posted_at=posted.date() if posted else None,
            description=mark_hidden(text, hidden)[:MAX_DESCRIPTION],
            ats_job_id=str(row["id"])[:100],
            source_ref=str(row.get("slug") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """The newest Remote OK postings (newest first), optionally only those posted since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("Remote OK API reply is not a list")
    out, seen = [], set()
    for x in map(_listing, rows[1:]):  # rows[0] is the legal notice
        if x is None or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        out.append(x)
    out.sort(key=lambda x: x.posted_at or datetime.min.date(), reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="remoteok",
        description="Remote OK (remoteok.com API): the ~100 newest remote jobs worldwide, with full descriptions",
        fetch=fetch,
        terms="API terms: link back to the posting on Remote OK and credit Remote OK as the source; don't use its "
        "logo. Single endpoint, no key; robots.txt allows / (Content-Signal ai-train=no). Personal use.",
    )
)
