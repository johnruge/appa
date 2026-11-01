"""Jobright.ai 2026-Software-Engineer-New-Grad: a GitHub README, regenerated several times a day, listing
new-grad software roles posted in the last ~7 days (it carries 2027-start roles; there is no 2027 repo).

appa reads the markdown table between the TABLE_START and TABLE_END comments. Columns are Company, Job
Title, Location, Work Model, Date Posted. A company cell of "↳" means "same company as the row above".
Links point to jobright.ai job pages, not the employer's ATS; the 24-hex jobright id is the posting id.
Dates are "Mon DD" with no year, so the year is inferred from today.

describe() fetches one jobright.ai job page (robots.txt allows /jobs/*) and reads its __NEXT_DATA__ JSON
(props.pageProps.dataSource.jobResult): the summary, responsibilities and qualifications, plus jobright's
salary / employment-type / citizenship / sponsorship / clearance tags as plain lines. Personal fields on the
page (socialConnections, jobRecruiter) are never read.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from appa.core.locations import outside_us
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_text
from appa.sources.text import html_to_text, mark_hidden

URL = "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md"

_ROW = re.compile(
    r"^\|\s*(?P<company>.+?)\s*\|\s*\*\*\[(?P<title>.+?)\]\((?P<link>https://jobright\.ai/jobs/info/(?P<id>[0-9a-f]{24})"
    r"[^)\s]*)\)\*\*\s*\|\s*(?P<location>[^|]*?)\s*\|\s*(?P<model>[^|]*?)\s*\|\s*(?P<date>[^|]*?)\s*\|\s*$"
)
_ID = re.compile(r"[0-9a-f]{24}")
_NEXT_DATA = re.compile(r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
MAX_DESCRIPTION = 100_000
_COMPANY = re.compile(r"^\*\*\[(?P<name>.+)\]\([^)\s]*\)\*\*$")
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "on site": "no"}
_US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
_COUNTRIES = {
    "canada",
    "united kingdom",
    "uk",
    "ireland",
    "germany",
    "france",
    "netherlands",
    "spain",
    "portugal",
    "italy",
    "switzerland",
    "sweden",
    "poland",
    "israel",
    "india",
    "china",
    "japan",
    "singapore",
    "australia",
    "mexico",
    "brazil",
}
_MONTHS = {
    m: i
    for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)
}


def _date(text: str, today: date) -> date | None:
    """A "Mon DD" date, in the latest year that doesn't put it more than a day after today."""
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})", text.strip())
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    try:
        d = date(today.year, _MONTHS[m.group(1).lower()], int(m.group(2)))
        if (d - today).days > 1:  # "Dec 31" read on Jan 1 is last year's
            d = date(today.year - 1, d.month, d.day)
        return d
    except ValueError:
        return None


def _country(location: str) -> str:
    """The country the location names, or "" ("Hawthorne, CA" names a state, not Canada)."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    if last.lower() in _US_NAMES:
        return "United States"
    return last if len(parts) >= 3 or last.lower() in _COUNTRIES else ""


def _company(cell: str) -> str:
    """The company name from a "**[Name](site)**" cell (or the bare text)."""
    m = _COMPANY.match(cell.strip())
    if m:
        return m.group("name").strip()
    return "" if "](" in cell else cell.replace("**", "").strip()


def parse(text: str, today: date) -> list[Listing]:
    """Every well-formed row of the README's job table, carrying "↳" companies forward."""
    start, end = text.find("TABLE_START"), text.find("TABLE_END")
    body = text[start:end] if 0 <= start < end else text
    out: list[Listing] = []
    company = ""
    for line in body.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        cell = m.group("company").strip()
        if cell != "↳":
            company = _company(cell)
        if not company or not m.group("title").strip():
            continue
        location = m.group("location")
        hints = {
            "level": "new_grad",
            "category": "Software Engineering",
            "remote": _REMOTE.get(m.group("model").strip().lower(), ""),
        }
        if country := _country(location):
            hints["country"] = country
        job_id = m.group("id")
        try:
            out.append(
                Listing(
                    company=company,
                    title=m.group("title"),
                    url=f"https://jobright.ai/jobs/info/{job_id}",
                    location=location,
                    posted_at=_date(m.group("date"), today),
                    ats="jobright",
                    ats_job_id=job_id,
                    source_ref=job_id,
                    hints=hints,
                )
            )
        except ValueError:
            continue
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """The README's current rows (newest first): US-only when asked, and only since opts.since if set."""
    text, _ = get_text(URL)
    out, seen = [], set()
    for x in parse(text, datetime.now(UTC).date()):
        if x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        if opts.us_only and outside_us(x.location, x.hints.get("country")):
            continue
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


def _items(value) -> list[str]:
    """The non-empty strings of a list field (anything else gives [])."""
    return [str(x) for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def _flag(value) -> str:
    """ "yes"/"no" for a boolean tag, "" when the page doesn't say."""
    return {True: "yes", False: "no"}.get(value, "") if isinstance(value, bool) else ""


def page_text(page: str) -> str:
    """The posting text from a jobright.ai job page's __NEXT_DATA__ JSON; "" when it has none."""
    m = _NEXT_DATA.search(page)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
        job = data["props"]["pageProps"]["dataSource"]["jobResult"]
    except (ValueError, KeyError, TypeError):
        return ""
    if not isinstance(job, dict):
        return ""
    quals = job.get("qualifications") if isinstance(job.get("qualifications"), dict) else {}
    parts = []
    if isinstance(job.get("jobSummary"), str) and job["jobSummary"].strip():
        parts.append(f"<p>{job['jobSummary']}</p>")
    for heading, items in (
        ("Responsibilities", _items(job.get("coreResponsibilities"))),
        ("Required qualifications", _items(quals.get("mustHave"))),
        ("Preferred qualifications", _items(quals.get("preferredHave"))),
    ):
        if items:
            parts.append(f"<h3>{heading}</h3><ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
    tags = [
        (label, value)
        for label, value in (
            ("Salary", job.get("salaryDesc") if isinstance(job.get("salaryDesc"), str) else ""),
            ("Employment type", job.get("employmentType") if isinstance(job.get("employmentType"), str) else ""),
            ("US citizenship required", _flag(job.get("isCitizenOnly"))),
            ("H-1B sponsorship", _flag(job.get("isH1bSponsor"))),
            ("Security clearance required", _flag(job.get("isClearanceRequired"))),
            ("Work authorization required", _flag(job.get("isWorkAuthRequired"))),
        )
        if value.strip()
    ]
    if tags:
        parts.append("<h3>Jobright tags</h3><ul>" + "".join(f"<li>{k}: {v}</li>" for k, v in tags) + "</ul>")
    if not parts:
        return ""
    text, hidden = html_to_text("".join(parts))
    return mark_hidden(text, hidden)[:MAX_DESCRIPTION]


def describe(listing: Listing) -> str:
    """The posting text from its jobright.ai job page (one request); "" when the id is not a jobright id."""
    job_id = listing.ats_job_id or ""
    if not _ID.fullmatch(job_id):
        return ""
    page, _ = get_text(f"https://jobright.ai/jobs/info/{job_id}")
    return page_text(page)


FEED = register_list(
    ListFeed(
        name="jobright_swe_newgrad",
        description="Jobright.ai 2026-Software-Engineer-New-Grad (GitHub): new-grad SWE roles from the last ~7 days, "
        "several updates a day; links are jobright.ai pages",
        fetch=fetch,
        describe=describe,
        terms="Public GitHub repository (no license file); personal use. Links go to jobright.ai job pages (robots.txt "
        "allows /jobs/*); describe() reads one page per posting. Open the page to reach the employer's apply link.",
    )
)
