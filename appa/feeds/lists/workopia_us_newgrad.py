"""workopia/US-New-Grad-Internship-Jobs: a daily GitHub list of US graduate, entry-level and intern roles
published by the Workopia job aggregator. appa reads the repo's listings.json from raw.githubusercontent.com
(about 1,100 rows, at most 80 per category and tier) and keeps the Tech, Data/AI/ML and New Grad Programs
categories at the Graduate and Entry-level tiers (internships are dropped). New Grad Programs is mostly
non-software (apprentices, retail and manager trainees), so its rows are kept only when the title names
software, data, engineering or IT work.

Each row links to a workopia.io/jobs/<id> aggregator page, not the employer's ATS (it 403s plain requests),
so there's no direct link and no description. appa builds the url itself from the validated Workopia id
(rows without a valid id are skipped), uses the id as ats_job_id and dedupes on it only: the same company
and title with different ids are separate postings. Mainly useful for discovering companies; low priority. A salary
marked "*" is Workopia's
own estimate, not posted pay, so it isn't passed on as a salary hint.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/workopia/US-New-Grad-Internship-Jobs/main/.github/scripts/listings.json"
CATEGORIES = {"tech": "Tech", "data": "Data, AI & ML", "grad": "New Grad Programs"}
LEVELS = {"Graduate": "new_grad", "Entry-level": "entry"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_TECH = re.compile(
    r"\b(software|developer|engineer\w*|data|machine learning|ml|ai|computer|cyber\w*|security|it|"
    r"devops|cloud|swe|sde|programm\w*|technolog\w*|analytics|quantitative|systems?)\b",
    re.I,
)


def _date(value) -> date | None:
    """An ISO timestamp from the file, as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _listing(row) -> Listing | None:
    """A Listing from one row, or None for rows that are inactive, out of scope or malformed."""
    if not isinstance(row, dict) or not row.get("active"):
        return None
    category, tier = str(row.get("category") or ""), str(row.get("tier") or "")
    if category not in CATEGORIES or tier not in LEVELS:
        return None
    job_id, company, title = str(row.get("id") or ""), str(row.get("company_name") or ""), str(row.get("title") or "")
    if not (_ID.match(job_id) and company.strip() and title.strip()):
        return None
    if category == "grad" and not _TECH.search(title):  # grad programs are mostly trades, retail and trainees
        return None
    hints = {"category": CATEGORIES[category], "level": LEVELS[tier], "country": "US"}
    salary = str(row.get("salary_display") or "")
    if salary and "*" not in salary:  # "*" marks a Workopia estimate, not the posted range
        hints["salary"] = salary
    if row.get("sponsorship") is True:  # False is the default for every row, so it says nothing
        hints["sponsorship"] = "Offers Sponsorship"
    location = str(row.get("location") or "")
    if "remote" in location.lower():
        hints["remote"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=f"https://workopia.io/jobs/{quote(job_id, safe='')}",
            location=location[:500],
            posted_at=_date(row.get("date_posted")),
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Active Tech/Data/New Grad rows at the Graduate and Entry-level tiers, newest first, one per Workopia id."""
    data = get_json(URL)
    rows = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Workopia listings file has no listings list")
    out = [x for x in map(_listing, rows) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    seen: set[str] = set()
    unique = []
    for x in out:
        k = x.ats_job_id or x.url
        if k not in seen:
            seen.add(k)
            unique.append(x)
    return unique[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="workopia_us_newgrad",
        description="workopia/US-New-Grad-Internship-Jobs (GitHub): daily US graduate and entry-level tech/data roles",
        fetch=fetch,
        terms="Public GitHub repo with no license file; personal use only. Links are workopia.io aggregator pages "
        "(not the employer's ATS) that block plain requests, so appa doesn't fetch them.",
    )
)
