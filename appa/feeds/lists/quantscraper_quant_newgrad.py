"""QuantScraper 2027 Quant Internships & Graduate Roles: a GitHub README (quant-scraper/
Quant-Internships-New-Grad-2027) regenerated every few hours from about 50 trading firms', hedge funds' and
market makers' own careers pages. There is no JSON, only Markdown pipe tables grouped by discipline
(### Trading / Quantitative research / Software & technology / Operations, risk & other), some split
further into <details> blocks by focus. appa reads only the "## Graduate roles" section (internships come
before it, programmes and insight weeks after it). Role links go straight to the firm's posting or ATS
(Greenhouse, Lever, Ashby, careers.sig.com...). A "↳" company cell repeats the company above, and Posted is
"Mon DD" (this year, or last year if that would be more than two days in the future; the README is
built in UTC), "Mon DD, YYYY" or "—". category comes
from the upstream discipline and Focus tag, which is sometimes wrong. A location ending in "+N" hides N more
offices, so when the visible ones are all abroad appa leaves location empty (one hidden office may be in the US)
unless the title itself names a non-US region such as "(Europe)".
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta

from appa.core.jobid import job_key
from appa.core.locations import outside_us
from appa.feeds import KNOWN_ATS, FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_text

URL = "https://raw.githubusercontent.com/quant-scraper/Quant-Internships-New-Grad-2027/main/README.md"
SECTION = re.compile(r"^## Graduate roles[ \t]*$", re.M)
TERMS = (
    "Public GitHub repository generated from quant-scraper.com (no license file); personal use only. "
    "One raw file per run; check with the maintainers before redistributing the data."
)

_LINK = re.compile(r"^\[(.+)\]\((https?://[^)\s]+)\)$")
_BOLD = re.compile(r"^\*\*(.+)\*\*$")
_SUMMARY = re.compile(r"<summary>\s*<b>(.*?)</b>", re.I)
_TAG = re.compile(r"<[^>]+>")
_EXTRA = re.compile(r"\s*\+\d+$")
_DASHES = {"", "—", "-", "–"}
_MON_DD = re.compile(r"([A-Za-z]{3}) (\d{1,2})")
_ABROAD = re.compile(r"\b(Europe|European|EMEA|APAC|Asia|Asia[- ]Pacific|Australia|UK|Hong Kong|Singapore)\b", re.I)
_SLACK = timedelta(days=2)


def _plain(cell: str) -> str:
    """A cell as plain text (tags and Markdown emphasis dropped, entities decoded)."""
    return " ".join(html.unescape(_TAG.sub(" ", cell)).replace("**", "").split())


def _cells(line: str) -> list[str]:
    """The cells of one Markdown pipe-table row."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _posted(text: str, today: date) -> date | None:
    """'Oct 29' (latest such date at most two days after today) or 'Jun 24, 2025' as a date; None for '—' or junk."""
    text = text.strip()
    try:
        return datetime.strptime(text, "%b %d, %Y").date()
    except ValueError:
        pass
    m = _MON_DD.fullmatch(text)
    if not m:
        return None
    try:
        month = datetime.strptime(m.group(1), "%b").month
    except ValueError:
        return None
    for year in range(today.year, today.year - 5, -1):  # five years reach the last Feb 29
        try:
            d = date(year, month, int(m.group(2)))
        except ValueError:
            continue
        if d <= today + _SLACK:
            return d
    return None


def _company(cell: str, previous: str) -> str:
    """The company in a Company cell: '**[Name](site)**', '**Name**', or '↳' for the row above's company."""
    cell = cell.strip()
    if cell.startswith("↳"):
        return previous
    bold = _BOLD.match(cell)
    inner = bold.group(1) if bold else cell
    link = _LINK.match(inner)
    return _plain(link.group(1) if link else inner)


def _location(cell: str, title: str = "") -> str:
    """The Location cell; '' when a '+N' suffix hides offices, the visible ones are abroad and the title names no
    region."""
    location = _plain(cell)
    if location in _DASHES:
        return ""
    visible = _EXTRA.sub("", location)
    if visible != location and outside_us(visible) and not _ABROAD.search(title):
        return ""
    return visible[:500]


def _listing(cell: dict[str, str], company: str, discipline: str, focus: str, today: date) -> Listing | None:
    """A Listing from one graduate-role row, or None when the row is malformed."""
    role = _LINK.match(cell.get("role", "").strip())
    if not (role and company):
        return None
    title, url = _plain(role.group(1)), html.unescape(role.group(2)).strip()
    location = _location(cell.get("location", ""), title)
    salary = _plain(cell.get("salary", ""))
    focus = _plain(cell.get("focus", "")) or focus
    key = job_key(url)
    scope, _, job_id = key.rpartition(":") if key else ("", "", "")
    hints = {
        "level": "new_grad",
        "employment_type": "full_time",
        "category": " / ".join(x for x in (discipline, focus) if x),
        "salary": "" if salary in _DASHES else salary,
    }
    if "remote" in location.lower():
        hints["remote"] = "yes"
    if re.search(r"citizenship|clearance", title, re.I):
        hints["citizenship_required"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=location,
            posted_at=_posted(cell.get("posted", ""), today),
            ats=scope if scope in KNOWN_ATS else "unknown",
            ats_job_id=job_id[:100] or None,
            hints=hints,
        )
    except ValueError:
        return None


def _parse(text: str, today: date) -> list[Listing]:
    """Every row of the '## Graduate roles' section, in file order (duplicate links dropped)."""
    section = SECTION.search(text.replace("\r\n", "\n"))
    if section is None:
        raise ValueError("QuantScraper README has no graduate roles section")
    out, seen = [], set()
    discipline = focus = company = ""
    header: list[str] = []
    for line in section.string[section.end() :].splitlines():
        if line.startswith("## "):
            break  # programmes & insight weeks follow
        if line.startswith("### "):
            discipline = _plain(line[4:].split("—")[0])
            focus, company, header = "", "", []
            continue
        summary = _SUMMARY.search(line)
        if summary:
            focus, company = _plain(summary.group(1)), ""
            continue
        if not line.startswith("|") or line.startswith("|---"):
            continue
        row = _cells(line)
        if row and row[0].lower() == "company":
            header = [_plain(c).lower() for c in row]
            continue
        if len(row) != len(header):
            continue
        cell = dict(zip(header, row, strict=False))
        company = _company(cell.get("company", ""), company)
        x = _listing(cell, company, discipline, focus, today)
        if x is not None and x.url not in seen:
            seen.add(x.url)
            out.append(x)
    return out


def _today() -> date:
    """Today's UTC date, matching the README's generator (a function so tests can pin it)."""
    return datetime.now(UTC).date()


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every 2027 quant graduate role (newest first), optionally only those posted since opts.since."""
    text, _ = get_text(URL)
    out = _parse(text, _today())
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="quantscraper_quant_newgrad",
        description="QuantScraper Quant-Internships-New-Grad-2027 (GitHub): graduate roles at ~50 quant firms, "
        "worldwide",
        fetch=fetch,
        terms=TERMS,
    )
)
