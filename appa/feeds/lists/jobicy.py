"""Jobicy: a free, keyless remote-jobs API (https://jobi.cy/apidocs). It returns remote postings published
in the last 7 days, newest first, with the full HTML description, 100 per page via an opaque nextCursor.
appa walks the tech categories (software, data, DevOps, QA, security), restricted to USA-eligible roles
when us_only is set. The free API's url is the Jobicy listing page (direct ATS links cost money), and
Jobicy asks that apply links keep pointing there and that it's credited as the source.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://jobicy.com/api/jobs"
INDUSTRIES = ("engineering", "data-science", "admin", "qa-testing", "cybersecurity")  # "admin" = DevOps
MAX_PAGES = 20  # per category; the 7-day window rarely needs more than a few
PAGE_SLEEP = 1.0
MAX_DESC = 100_000
_LEVELS = (
    ("intern", "intern"),
    ("entry", "entry"),
    ("junior", "entry"),
    ("mid", "mid"),
    ("senior", "senior"),
    ("lead", "senior"),
    ("director", "senior"),
    ("executive", "senior"),
)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def _date(s) -> date | None:
    """The ISO pubDate as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(s)).date()
    except (TypeError, ValueError):
        return None


def _labels(v) -> list[str]:
    """A field that is a list of labels (or, in older API versions, one string) as a clean list."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v or "").strip().strip("[]")
    return [x.strip(" '\"") for x in s.split(",") if x.strip(" '\"")]


def _level(raw: str, types: list[str]) -> str:
    """Jobicy's jobLevel (and an Internship job type) mapped to a HINT_KEYS level, or '' when unstated."""
    if any("intern" in t.lower() for t in types):
        return "intern"
    raw = raw.lower()
    return next((v for k, v in _LEVELS if k in raw), "")


def _salary(row: dict) -> str:
    """The posted pay range as text, e.g. 'USD 175,000-220,000 yearly' ('' when not given)."""
    lo, hi = row.get("salaryMin"), row.get("salaryMax")
    try:
        lo, hi = (f"{int(float(x)):,}" if x not in (None, "") else "" for x in (lo, hi))
    except (TypeError, ValueError):
        return ""
    if not (lo or hi):
        return ""
    rng = f"{lo}-{hi}" if lo and hi else f"{lo}+" if lo else f"up to {hi}"
    return " ".join(x for x in (str(row.get("salaryCurrency") or ""), rng, str(row.get("salaryPeriod") or "")) if x)


def _links(raw_html: str) -> str:
    """Non-Jobicy http(s) links in the description, space separated (for board discovery)."""
    out: list[str] = []
    for h in _HREF.findall(raw_html):
        h = h.strip()
        host = re.sub(r"^https?://", "", h, flags=re.I).split("/")[0].split("?")[0].lower()
        if h.lower().startswith(("http://", "https://")) and not (host == "jobicy.com" or host.endswith(".jobicy.com")):
            if h not in out:
                out.append(h)
    return " ".join(out[:10])


def _listing(row: dict) -> Listing | None:
    """A Listing from one job in the API response, or None for malformed rows."""
    if not isinstance(row, dict):
        return None
    jid = str(row.get("id") or "").strip()
    url = str(row.get("url") or "")
    raw = str(row.get("jobDescription") or "")
    text, hidden = html_to_text(raw) if raw else ("", [])
    types = _labels(row.get("jobType"))
    geo = " ".join(str(row.get("jobGeo") or "").split())
    hints = {
        "remote": "yes",
        "country": geo,
        "category": ", ".join(_labels(row.get("jobIndustry"))),
        "level": _level(str(row.get("jobLevel") or ""), types),
        "employment_type": ", ".join(t.lower().replace("-", "_").replace(" ", "_") for t in types),
        "salary": _salary(row),
        "links": _links(raw),
    }
    try:
        return Listing(
            company=str(row.get("companyName") or ""),
            title=str(row.get("jobTitle") or ""),
            url=url,
            location=f"Remote ({geo})" if geo else "Remote",
            posted_at=_date(row.get("pubDate")),
            description=mark_hidden(text, hidden)[:MAX_DESC],
            ats_job_id=jid or None,
            source_ref=jid or None,
            hints=hints,
        )
    except ValueError:
        return None


def _category(industry: str, opts: FeedOptions, limit: int) -> list[Listing]:
    """One category's postings, following nextCursor until the end, the cap, or postings older than since."""
    params: dict = {"count": 100, "industry": industry}
    if opts.us_only:
        params["geo"] = "usa"
    out: list[Listing] = []
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAGE_SLEEP)
        data = get_json(URL, params=params)
        if not isinstance(data, dict) or data.get("success") is False:
            raise ValueError(f"Jobicy API error: {str(data.get('error') if isinstance(data, dict) else data)[:200]}")
        rows = data.get("jobs") or []
        if not isinstance(rows, list) or not rows:
            break
        items = [x for x in map(_listing, rows) if x is not None]
        out += [x for x in items if not opts.since or x.posted_at is None or x.posted_at >= opts.since]
        older = opts.since and any(x.posted_at and x.posted_at < opts.since for x in items)
        cursor = data.get("nextCursor")
        if len(out) >= limit or older or not cursor or not isinstance(cursor, str):
            break
        params = {**params, "cursor": cursor}
    return out[:limit]


def fetch(opts: FeedOptions) -> list[Listing]:
    """Remote tech postings from the last 7 days across Jobicy's tech categories, deduped by Jobicy ID."""
    seen: set[str] = set()
    out: list[Listing] = []
    for i, industry in enumerate(INDUSTRIES):
        if len(out) >= opts.max_results:
            break
        if i:
            time.sleep(PAGE_SLEEP)
        for x in _category(industry, opts, opts.max_results - len(out)):
            k = x.ats_job_id or x.url
            if k not in seen:
                seen.add(k)
                out.append(x)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="jobicy",
        description="Jobicy remote jobs API: remote software/data/DevOps/QA/security roles from the last 7 days",
        fetch=fetch,
        terms=(
            "Free, no key (https://jobi.cy/apidocs). Credit Jobicy as the source and keep apply links pointing to "
            "the Jobicy listing. Poll a few times a day; start a new pass at most once per hour."
        ),
    )
)
