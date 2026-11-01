"""DereC4 internships-and-newgrad: a GitHub repo (MIT) by Derek Chen that re-aggregates other new-grad
lists (Zapply, Simplify and others) into one Markdown table in NEWGRAD.md, between comment markers:

    | Company | Role | Location | Age |
    | Roblox | [\\[2027\\] Software Engineer ...](https://zapply.jobs/l/d/greenhouse-roblox-8315883?s=...) | ... | 0d |

There's no JSON, so appa parses the table. The Role cell links the posting: about a third are Zapply
redirects (decoded exactly as the zapply_newgrad2027 feed does, so they dedupe with it and Greenhouse,
Lever and Ashby rows get their direct ATS link), the rest go straight to Greenhouse, Ashby, Lever,
Workday, TikTok... "Age" is relative ("0d", "3d", "1mo"), an ISO date or "Date unknown". The file keeps
stale rows (months old) and some internships, so only rows aged MAX_AGE_DAYS or less and non-intern
titles are kept; posted_at is approximate.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

from appa.core.jobid import job_key
from appa.feeds import KNOWN_ATS, FeedOptions, ListFeed, Listing, register_list
from appa.feeds.lists.zapply_newgrad2027 import _CLEARANCE, _INTERN, _ats
from appa.sources.http import get_text

URL = "https://raw.githubusercontent.com/DereC4/internships-and-newgrad/main/NEWGRAD.md"
START, END = "<!-- NEWGRAD_TABLE_START_DEREXXD -->", "<!-- NEWGRAD_TABLE_END_DEREXXD -->"
MAX_AGE_DAYS = 31  # "1mo" and newer; older rows are mostly closed

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_ROLE = re.compile(r"^\[(.+)\]\((https?://[^\s)]+)\)$", re.S)
_ZAPPLY = re.compile(r"^https://zapply\.jobs/l/d/([A-Za-z0-9][A-Za-z0-9._-]{0,199})(?:[/?#].*)?$")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")
_AGE = re.compile(r"^(\d{1,4})\s*(mo|d|w|h|y)$", re.I)
_SECRET = re.compile(r"\bSECRET\b|\bclearance\b", re.I)


def _clean(cell: str) -> str:
    """A table cell as plain text: no bold markers, markdown escapes or HTML entities."""
    return html.unescape(_MD_ESCAPE.sub(r"\1", cell.replace("**", ""))).strip()


def _posted(age: str, today: date) -> date | None:
    """The posting date from an age cell ('0d', '3d', '1mo', '2026-08-02'); None for 'Date unknown'."""
    age = age.strip()
    try:
        return date.fromisoformat(age)
    except ValueError:
        pass
    m = _AGE.match(age)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return today - timedelta(days={"h": n // 24, "d": n, "w": 7 * n, "mo": 30 * n, "y": 365 * n}[unit])


def _listing(line: str, today: date) -> Listing | None:
    """A Listing from one table row, or None for headers, internships and malformed rows."""
    cells = _CELL_SPLIT.split(line.strip())
    if len(cells) != 6:
        return None
    company, role, location, age = (c.strip() for c in cells[1:5])
    m = _ROLE.match(role)
    if not m:
        return None
    title, link = _clean(m.group(1)), html.unescape(m.group(2)).strip()
    company, location = _clean(company), _clean(location)
    if not company or not title or _INTERN.search(title):
        return None
    hints = {"level": "new_grad", "employment_type": "full_time"}
    z = _ZAPPLY.match(link)
    if z:
        slug = z.group(1)
        ats, jid, direct = _ats(slug)
        zapply = f"https://zapply.jobs/l/d/{quote(slug, safe='')}"
        url, ref = direct or zapply, slug
        if direct:
            hints["links"] = zapply
    else:
        key = job_key(link)
        scope, _, jid = key.rpartition(":") if key else ("", "", "")
        ats, url, ref = scope if scope in KNOWN_ATS else "unknown", link, None
    if _CLEARANCE.search(title) or _SECRET.search(title):
        hints["citizenship_required"] = "yes"
    if "remote" in f"{title} {location}".lower():
        hints["remote"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=location,
            posted_at=_posted(age, today),
            ats=ats,
            ats_job_id=jid[:100] or None,
            source_ref=ref,
            hints=hints,
        )
    except ValueError:
        return None


def parse(text: str, today: date | None = None) -> list[Listing]:
    """Every non-internship row of the NEWGRAD table, stale and undated rows included."""
    today = today or datetime.now(UTC).date()
    i = text.find(START)
    j = text.find(END, i + len(START)) if i >= 0 else -1
    if i < 0 or j < 0:
        raise ValueError("DereC4 NEWGRAD.md has no job table")
    out = []
    for line in text[i + len(START) : j].splitlines():
        if line.lstrip().startswith("| ") and "](http" in line:
            x = _listing(line, today)
            if x is not None:
                out.append(x)
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """Rows aged MAX_AGE_DAYS or less (and since opts.since), newest first, deduped by posting key (newest kept)."""
    text, _ = get_text(URL)
    today = datetime.now(UTC).date()
    cutoff = max(today - timedelta(days=MAX_AGE_DAYS), opts.since or date.min)
    seen: set[str] = set()
    out: list[Listing] = []
    rows = sorted(parse(text, today), key=lambda x: x.posted_at or date.min, reverse=True)
    for x in rows:
        if x.posted_at is None or x.posted_at < cutoff or x.key in seen:
            continue
        seen.add(x.key)
        out.append(x)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="derec4_newgrad",
        description="DereC4 internships-and-newgrad (GitHub): re-aggregated new-grad roles from Zapply, Simplify "
        "and others; last ~month only",
        fetch=fetch,
        terms="Public GitHub repository (MIT, Derek Chen). One raw file per run. Rows re-aggregate other lists "
        "(Zapply's data: credit Zapply, don't resubmit it to other aggregators).",
    )
)
