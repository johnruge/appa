"""Tar-ive ML_Newgrad_2027: a GitHub meta-list of 2027 new-grad ML, data-science, SWE-with-ML and quant
roles (new grad through ~3 years), refreshed hourly. It merges other lists (Simplify, SpeedyApply,
coconight, zapply, dreamworkhq...) plus ATS crawls into one machine-readable file (data/listings.json,
a few thousand rows). appa keeps rows the file marks active and in the U.S., skips those its own checker
found closed, and links straight to the employer's posting. A row's urls[] can hold several postings of
the same role (one per location or requisition); each distinct direct posting becomes its own Listing,
and aggregator links (zapply, dreamworkhq, short links) are used only when a row has nothing direct.
The file has no posting text, so the scout reads the employer page later.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/Tar-ive/ML_Newgrad_2027/main/data/listings.json"
# Sites that relist other sites' postings; a link to one is a fallback, never the posting itself.
AGGREGATORS = (
    "zapply.jobs",
    "dreamworkhq.com",
    "go.vansh.nyc",
    "simplify.jobs",
    "indeed.com",
    "jobleads.com",
    "tealhq.com",
    "careerbuilder.com",
    "welcometothejungle.com",
    "tsenta.com",
    "jobright.ai",
)
FAMILIES = {
    "ml": "Machine Learning",
    "ds": "Data Science",
    "swe_ml": "Software (ML)",
    "quant": "Quant",
    "de_ml": "Data Engineering (ML)",
}
MAX_PER_ROW = 10
# The file's own classifier labels, not role categories; FAMILIES[family] is used instead.
INTERNAL_TAGS = {"ai_first", "ai_explicit", "non_ai", "quant", "other"}
AMAZON_APPLY = re.compile(r"^https?://account\.amazon\.jobs/jobs/(\d{1,20})(?:/apply)?/?$", re.I)
ASHBY_APPLY = re.compile(
    r"^(https://jobs\.ashbyhq\.com/[A-Za-z0-9._%-]+/[0-9a-fA-F-]{36})/application/?(?:\?embed=true)?$"
)
YEARS = re.compile(r"^\s*(\d{1,2})")
NEW_GRAD = re.compile(r"\b(new grad|new graduate|entry[- ]level|early career|university grad|junior)\b", re.I)


def _date(ts) -> date | None:
    """A unix timestamp from the file as a UTC date (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _aggregator(url: str) -> bool:
    """True when the link's host is (a subdomain of) a known aggregator."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in AGGREGATORS)


def _ats(url: str) -> tuple[str, str | None]:
    """(ats, job id) read from the link: Greenhouse/Lever/Ashby by name, Workday by host, else unknown."""
    ref = parse_url(url)
    if ref and ref.job_id:
        return ref.ats, ref.job_id[:100]
    key = job_key(url)
    jid = key.split(":", 1)[1][:100] if key else None
    return ("workday" if key and "myworkdayjobs.com:" in key else "unknown"), jid


def _canonical(url: str) -> str:
    """The public posting page for known apply/embed links (Amazon login apply page, Ashby embedded form)."""
    m = AMAZON_APPLY.match(url)
    if m:
        return f"https://www.amazon.jobs/en/jobs/{m.group(1)}"
    m = ASHBY_APPLY.match(url)
    return m.group(1) if m else url


def _links(row: dict) -> list[str]:
    """The row's distinct postings, best first: direct links (apply_url, url, then urls[]) deduped by job
    identity; the first aggregator link only when there is no direct one."""
    raw = [row.get("apply_url"), row.get("url"), *(row.get("urls") if isinstance(row.get("urls"), list) else [])]
    seen, direct, other = set(), [], []
    for u in raw:
        u = str(u or "").strip()
        if not u.lower().startswith(("http://", "https://")) or len(u) > 2000:
            continue
        u = _canonical(u)
        k = job_key(u) or u.rstrip("/")
        if k in seen:
            continue
        seen.add(k)
        (other if _aggregator(u) else direct).append(u)
    return direct[:MAX_PER_ROW] if direct else other[:1]


def _level(row: dict, ver: dict) -> str:
    """The seniority the file states: 'mid' when too senior or 2+ years, 'new_grad' for 0 years or a new-grad
    title, else '' (unstated)."""
    if ver.get("status") == "too_senior":
        return "mid"
    if NEW_GRAD.search(str(row.get("title") or "")):
        return "new_grad"
    m = YEARS.match(str(ver.get("years") or ""))
    if not m:
        return ""
    return "new_grad" if int(m.group(1)) == 0 else "mid" if int(m.group(1)) >= 2 else ""


def _hints(row: dict, locations: list[str]) -> dict[str, str]:
    """The facts the file states about a row, as HINT_KEYS hints."""
    ver = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    salary = str(row.get("salary") or "")
    category = str(row.get("category") or "")
    if category.lower() in INTERNAL_TAGS:
        category = ""
    hints = {
        "category": category or FAMILIES.get(str(row.get("family") or ""), ""),
        "level": _level(row, ver),
        "country": "US",
        "salary": "" if salary.lower() == "not listed" else salary,
    }
    if any("remote" in x.lower() for x in locations):
        hints["remote"] = "yes"
    return hints


def _listings(row: dict) -> list[Listing]:
    """Listings for one row: none for rows that are inactive, outside the U.S., found closed or malformed."""
    if not isinstance(row, dict) or row.get("active") is not True or row.get("is_usa") is not True:
        return []
    ver = row.get("verification")
    if isinstance(ver, dict) and ver.get("status") == "closed":
        return []
    locations = [str(x) for x in row.get("locations") or [] if x] if isinstance(row.get("locations"), list) else []
    hints = _hints(row, locations)
    posted = _date(row.get("date_posted")) or _date(row.get("first_seen"))
    links = _links(row)
    out = []
    for url in links:
        ats, jid = _ats(url)
        others = " ".join(x for x in links if x != url)
        try:
            out.append(
                Listing(
                    company=str(row.get("company") or ""),
                    title=str(row.get("title") or ""),
                    url=url,
                    location="; ".join(locations)[:500],
                    posted_at=posted,
                    ats=ats,
                    ats_job_id=jid,
                    source_ref=str(row.get("id") or "")[:300] or None,
                    hints={**hints, "links": others},
                )
            )
        except ValueError:
            continue
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every active U.S. posting (newest first, one per distinct link), optionally only those since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("ML_Newgrad_2027 listings file is not a list")
    out, seen = [], set()
    for row in rows:
        for x in _listings(row):
            if x.key not in seen:
                seen.add(x.key)
                out.append(x)
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="tarive_ml_newgrad",
        description="Tar-ive ML_Newgrad_2027 (GitHub): merged new-grad ML/DS/SWE-ML/quant roles in the U.S., hourly",
        fetch=fetch,
        terms="Public GitHub repository (no license file); it republishes other lists' data. Personal use; "
        "don't redistribute the data.",
    )
)
