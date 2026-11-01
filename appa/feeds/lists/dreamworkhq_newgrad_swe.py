"""dreamworkhq New-Grad-Software-Engineer-Jobs: entry-level US software, data and security roles (no
senior titles, no internships) that Dreamwork crawls from company career pages and publishes daily as
one JSON file on GitHub (thousands of rows, most with a posted salary range).

Rows link to Dreamwork's own job page (dreamworkhq.com/job/<uuid>), not the employer's ATS, so the
uuid is the posting id and cross-source duplicates aren't caught by job id. describe() reads that page
(robots.txt allows /job/): the schema.org JobPosting text (or the page's visible description), plus the "View
original posting" ATS link.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

URL = "https://raw.githubusercontent.com/dreamworkhq/New-Grad-Software-Engineer-Jobs/main/data/listings.json"
JOB = "https://www.dreamworkhq.com/job/"
MAX_DESC = 100_000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no"}
_LDJSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_ANCHOR = re.compile(r"<a\s[^>]{0,2000}>", re.I)
_CTA = re.compile(r'\sclass="[^"]*\bjob-cta-secondary\b[^"]*"', re.I)
_HREF = re.compile(r'\shref="(https?://[^"\s<>]{1,1000})"', re.I)
_BODY = re.compile(
    r'<div[^>]*class="[^"]*\bjob-description-content\b[^"]*"[^>]*>(.*?)(?:<nav\b|<footer\b|<script\b|$)', re.S | re.I
)
_US = re.compile(r"\b(?:US|USA|U\.S\.)(?![A-Za-z])|\b(?i:united states)\b")


def _date(ts) -> date | None:
    """An ISO timestamp from the file as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date() if ts else None
    except ValueError:
        return None


def _salary(row: dict) -> str:
    """The posted pay range, e.g. '$121,830-$154,500/yr' ('' when the posting has none)."""
    lo, hi = row.get("salaryMin"), row.get("salaryMax")
    nums = [f"${int(x):,}" for x in (lo, hi) if isinstance(x, (int, float)) and x > 0]
    if not nums:
        return ""
    unit = {"annual": "/yr", "hourly": "/hr"}.get(str(row.get("salaryPeriod") or ""), "")
    return "-".join(dict.fromkeys(nums)) + unit


def _listing(row: dict) -> Listing | None:
    """A Listing from one row of the file, or None for malformed rows."""
    if not isinstance(row, dict):
        return None
    jid = str(row.get("id") or "").lower()
    if not _UUID.match(jid):
        return None
    location = str(row.get("location") or "")
    hints = {
        "level": "new_grad",
        "employment_type": "full_time",
        "salary": _salary(row),
        "remote": _REMOTE.get(str(row.get("remoteType") or ""), ""),
    }
    if _US.search(location):
        hints["country"] = "United States"
    domain = str(row.get("companyDomain") or "")
    if re.fullmatch(r"[a-z0-9.-]{1,200}\.[a-z]{2,}", domain):
        hints["links"] = f"https://{domain}"
    try:
        return Listing(
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            url=JOB + jid,
            location=location[:500],
            posted_at=_date(row.get("postedAt")),
            ats_job_id=jid,
            source_ref=jid,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every listing in the file (newest first, undated ones by first-indexed date), capped at max_results."""
    data = get_json(URL)
    rows = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("dreamworkhq listings file has no listings array")
    out, seen = [], set()
    for row in rows:
        x = _listing(row)
        if x is not None and x.ats_job_id not in seen:
            seen.add(x.ats_job_id)
            # undated rows sort (and filter) by when Dreamwork first saw them, an upper bound on posting
            out.append((x.posted_at or _date(row.get("firstIndexedAt")) or date.min, x))
    if opts.since:
        out = [(d, x) for d, x in out if d == date.min or d >= opts.since]
    out.sort(key=lambda dx: dx[0], reverse=True)
    return [x for _, x in out[: opts.max_results]]


def _posting(page: str) -> dict:
    """The schema.org JobPosting object embedded in a Dreamwork job page ({} if none)."""
    for raw in _LDJSON.findall(page):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return d
    return {}


def _original(page: str) -> str:
    """The employer's link from the page's "View original posting" anchor ('' if none or Dreamwork's own)."""
    for tag in _ANCHOR.findall(page):
        m = _HREF.search(tag)
        if _CTA.search(tag) and m:
            host = urlsplit(m.group(1)).hostname or ""
            if host and host != "dreamworkhq.com" and not host.endswith(".dreamworkhq.com"):
                return m.group(1)
    return ""


def describe(listing: Listing) -> str:
    """The posting text from the Dreamwork job page, with the employer's original link when shown."""
    jid = listing.ats_job_id or ""
    if not _UUID.match(jid):
        return ""
    page, _ = get_text(JOB + jid)
    raw = _posting(page).get("description")
    if not isinstance(raw, str) or not raw.strip():
        m = _BODY.search(page)  # some pages (e.g. Google's) have no JobPosting ld+json
        raw = m.group(1) if m else ""
    if not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    link = _original(page)
    if link:
        text += f"\n\nOriginal posting: {link}"
    return mark_hidden(text, hidden)[:MAX_DESC]


FEED = register_list(
    ListFeed(
        name="dreamworkhq_newgrad_swe",
        description="dreamworkhq New-Grad-Software-Engineer-Jobs (GitHub): entry-level US SWE/data/security "
        "roles crawled from career pages, daily, with salary; links go to dreamworkhq.com",
        fetch=fetch,
        describe=describe,
        terms="Public GitHub repository, MIT licensed (keep the Dreamwork notice if redistributed). Job pages are "
        "allowed by "
        "dreamworkhq.com robots.txt (/api/ is not); fetch them only for postings that pass filters.",
    )
)
