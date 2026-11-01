"""SpeedyApply 2027 College Jobs: curated, frequently updated lists of US new-grad roles kept on GitHub
(speedyapply/2027-SWE-College-Jobs and its sibling 2027-AI-College-Jobs). There is no JSON, only a Markdown
file with three HTML-ish pipe tables (FAANG+, Quant, Other) between comment markers. Each row links the
company site and a direct apply link (Greenhouse, Workday, amazon.jobs...) and shows the posting's age in
days, so posted_at is the fetch date minus that age. One adapter, two feeds: SWE and AI/ML new grad.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

from appa.core.jobid import job_key
from appa.feeds import KNOWN_ATS, FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_text

SWE_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"
AI_URL = "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/NEW_GRAD_USA.md"
SECTIONS = (("TABLE_FAANG", "FAANG+"), ("TABLE_QUANT", "Quant"), ("TABLE", "Other"))
TERMS = (
    "Public GitHub repository (no license file); personal use only. One raw file per run; "
    "check with the maintainers before redistributing the data."
)

_HREF = re.compile(r'<a\s+href="([^"]+)"', re.I)
_STRONG = re.compile(r"<strong>(.*?)</strong>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_AGE = re.compile(r"^(\d{1,4})d$")
_EXTRA = re.compile(r"\s*\+\d+$")


def _plain(cell: str) -> str:
    """A table cell as plain text (tags dropped, entities decoded)."""
    return " ".join(html.unescape(_TAG.sub(" ", cell)).split())


def _cells(line: str) -> list[str]:
    """The cells of one Markdown pipe-table row."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(text: str) -> list[tuple[str, list[str], list[list[str]]]]:
    """(section, header, rows) for each marked table in the file."""
    out = []
    for marker, section in SECTIONS:
        start, end = f"<!-- {marker}_START -->", f"<!-- {marker}_END -->"
        i = text.find(start)
        j = text.find(end, i + len(start)) if i >= 0 else -1
        if i < 0 or j < 0:
            continue
        header, rows = [], []
        for line in text[i + len(start) : j].splitlines():
            if line.startswith("| Company"):
                header = [_plain(c).lower() for c in _cells(line)]
            elif line.startswith("| <a") and header:
                rows.append(_cells(line))
        out.append((section, header, rows))
    return out


def _listing(section: str, track: str, header: list[str], row: list[str], today: date) -> Listing | None:
    """A Listing from one table row, or None when the row is malformed."""
    if len(row) != len(header):
        return None
    cell = dict(zip(header, row, strict=False))
    name = _STRONG.search(cell.get("company", ""))
    apply = _HREF.search(cell.get("posting", ""))
    company = _plain(name.group(1)) if name else _plain(cell.get("company", ""))
    title, location = _plain(cell.get("position", "")), _plain(cell.get("location", ""))
    if not (apply and company and title):
        return None
    url = html.unescape(apply.group(1)).strip()
    age = _AGE.match(_plain(cell.get("age", "")))
    key = job_key(url) if url.startswith(("http://", "https://")) else None
    scope, _, job_id = key.rpartition(":") if key else ("", "", "")
    hints = {
        "level": "new_grad",
        "employment_type": "full_time",
        "country": "US",
        "category": f"{track} / {section}",
        "salary": _plain(cell.get("salary", "")),
    }
    if location.lower().startswith("remote"):
        hints["remote"] = "yes"
    if re.search(r"citizenship|clearance", title, re.I):
        hints["citizenship_required"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=_EXTRA.sub("", location),
            posted_at=today - timedelta(days=int(age.group(1))) if age else None,
            ats=scope if scope in KNOWN_ATS else "unknown",
            ats_job_id=job_id[:100] or None,
            hints=hints,
        )
    except ValueError:
        return None


def _fetch(url: str, track: str, opts: FeedOptions) -> list[Listing]:
    """Every row of one SpeedyApply file (newest first), optionally only those posted since opts.since."""
    text, _ = get_text(url)
    if "TABLE_START" not in text:
        raise ValueError("SpeedyApply file has no job table")
    today, out, seen = date.today(), [], set()
    for section, header, rows in _tables(text):
        for row in rows:
            x = _listing(section, track, header, row, today)
            if x is None or x.url in seen:
                continue
            if opts.since and x.posted_at and x.posted_at < opts.since:
                continue
            seen.add(x.url)
            out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


def fetch(opts: FeedOptions) -> list[Listing]:
    """SpeedyApply 2027 SWE new-grad roles in the USA."""
    return _fetch(SWE_URL, "SWE", opts)


def fetch_ai(opts: FeedOptions) -> list[Listing]:
    """SpeedyApply 2027 AI/ML new-grad roles in the USA."""
    return _fetch(AI_URL, "AI/ML", opts)


FEED = register_list(
    ListFeed(
        name="speedyapply_swe_newgrad",
        description="SpeedyApply 2027-SWE-College-Jobs (GitHub): USA new-grad SWE roles, FAANG+/Quant/Other",
        fetch=fetch,
        terms=TERMS,
    )
)

AI_FEED = register_list(
    ListFeed(
        name="speedyapply_ai_newgrad",
        description="SpeedyApply 2027-AI-College-Jobs (GitHub): USA new-grad AI/ML roles, FAANG+/Quant/Other",
        fetch=fetch_ai,
        terms=TERMS,
    )
)
