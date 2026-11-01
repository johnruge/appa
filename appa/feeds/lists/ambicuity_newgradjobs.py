"""ambicuity New-Grad-Jobs (NGJ): a GitHub Actions scraper that reads about 900 employers' public ATS
APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Oracle HCM, iCIMS...) plus Indeed every
30 minutes, keeps new-grad / entry-level roles in the US, Canada and India posted in the last 60 days,
and publishes static JSON on GitHub Pages at jobs.riteshrana.engineer. appa reads jobs-index.json
(thousands of rows, one request, no posting text), keeps the tech categories and drops closed rows.
Each row links straight to the employer's ATS posting. describe() reads the posting text from the
site's description shard for that job (descriptions/<first hex of the id>.json).
"""

from __future__ import annotations

import re
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

BASE = "https://jobs.riteshrana.engineer"
URL = f"{BASE}/jobs-index.json"
TECH = {
    "software_engineering",
    "backend",
    "frontend",
    "mobile",
    "infrastructure_sre",
    "data_engineering",
    "data_ml",
    "security",
    "hardware",
    "quant_finance",
    "engineering",
}
MAX_DESC = 100_000
SHARD_TTL = 1800  # the site regenerates every ~30 minutes
_JOB_ID = re.compile(r"^job_[0-9a-f]{20}$")
_shards: dict[str, tuple[float, dict]] = {}


def _date(s) -> date | None:
    """An ISO timestamp from the feed as a date (None if missing or invalid)."""
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _is_indeed(host: str) -> bool:
    """True for indeed.com and its country subdomains (www., in., ca.)."""
    return host == "indeed.com" or host.endswith(".indeed.com")


def _ats(url: str) -> tuple[str, str | None]:
    """(ats, job id) read from the employer link: Greenhouse/Lever/Ashby by name, Workday by host,
    Indeed by its jk parameter, else the ID-looking part of the link."""
    ref = parse_url(url)
    if ref and ref.job_id:
        return ref.ats, ref.job_id[:100]
    p = urlparse(url)
    if _is_indeed((p.hostname or "").lower()):
        jk = (parse_qs(p.query).get("jk") or [""])[0].strip()
        return "indeed", (jk[:100] or None)
    key = job_key(url)
    jid = key.split(":", 1)[1][:100] if key else None
    return ("workday" if key and "myworkdayjobs.com:" in key else "unknown"), jid


def _salary(comp) -> str:
    """The posted pay range as text, e.g. 'USD 106,300-206,200' ('' when the posting states none)."""
    if not isinstance(comp, dict):
        return ""
    lo, hi, cur = comp.get("min"), comp.get("max"), str(comp.get("currency") or "")
    try:
        nums = [f"{int(x):,}" for x in (lo, hi) if x not in (None, "")]
    except (TypeError, ValueError):
        return ""
    return f"{cur} {'-'.join(dict.fromkeys(nums))}".strip() if nums else ""


def _listing(row: dict) -> Listing | None:
    """A Listing from one feed row, or None for rows that are closed, non-tech or malformed."""
    if not isinstance(row, dict) or row.get("is_closed"):
        return None
    cat = row.get("category") if isinstance(row.get("category"), dict) else {}
    if cat.get("id") not in TECH:
        return None
    url = str(row.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
    location = str(row.get("location") or "")
    hints = {
        "level": "entry",
        "category": str(cat.get("name") or cat.get("id") or ""),
        "salary": _salary(row.get("comp")),
    }
    if flags.get("us_citizenship_required") is True:
        hints["citizenship_required"] = "yes"
    if flags.get("no_sponsorship") is True:
        hints["sponsorship"] = "Does Not Offer Sponsorship"
    if "remote" in location.lower():
        hints["remote"] = "yes"
    ats, jid = _ats(url)
    ref = str(row.get("job_id") or row.get("id") or "")
    try:
        return Listing(
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            url=url,
            location=location[:500],
            posted_at=_date(row.get("posted_at")),
            ats=ats,
            ats_job_id=jid,
            source_ref=ref[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every open tech listing in the index (newest first), optionally only those posted since opts.since."""
    data = get_json(URL)
    rows = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("New-Grad-Jobs index has no jobs list")
    out, seen = [], set()
    for x in map(_listing, rows):
        if x is not None and x.key not in seen:
            seen.add(x.key)
            out.append(x)
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


def _shard(c: str) -> dict:
    """One description shard ({job_id: text}), cached for SHARD_TTL seconds."""
    hit = _shards.get(c)
    if hit and time.monotonic() - hit[0] < SHARD_TTL:
        return hit[1]
    data = get_json(f"{BASE}/descriptions/{c}.json")
    data = data if isinstance(data, dict) else {}
    _shards[c] = (time.monotonic(), data)
    return data


def describe(listing: Listing) -> str:
    """The posting text NGJ published for this listing ('' when it has none, e.g. most Workday rows)."""
    jid = listing.source_ref or ""
    if not _JOB_ID.match(jid):
        return ""
    raw = _shard(jid[4]).get(jid)
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


FEED = register_list(
    ListFeed(
        name="ambicuity_newgradjobs",
        description="ambicuity New-Grad-Jobs (jobs.riteshrana.engineer): new-grad/entry roles scraped from ~900 "
        "employer ATS boards every 30 min; tech categories only",
        fetch=fetch,
        describe=describe,
        terms="Site terms allow automated access at moderate rates and invite use of the published data files. "
        "Posting text belongs to the employers; personal use only. Visa/citizenship flags are automated.",
    )
)
