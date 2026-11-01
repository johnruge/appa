"""Zapply New-Grad 2027 lists: two GitHub READMEs (New-Grad-Software-Engineering-Jobs-2027 and
New-Grad-Jobs-2027) maintained by Zapply and refreshed about every 10 minutes. Each README holds
<details> sections (Software Engineering, AI / ML, Security...) with a markdown table of the most
recent postings (about 100 rows per section, a rolling window):

    | **Company** | Role | Location | Posted | Visa | [<img ...>](https://zapply.jobs/l/d/<ats>-<tenant>-<id>?s=...) |

There's no JSON behind it, so appa parses the tables. The apply link redirects to the real ATS
posting, and its slug names the ATS and the job ID, so postings dedupe without a request; for
Greenhouse, Lever and Ashby the ATS posting link is built from the slug (so boards get discovered).
Despite the repo names, many rows are internships; those are tagged level/employment_type 'intern' so
appa's filter skips them visibly. "Posted" is relative ("17m", "21h", "2d", "1mo"), so posted_at is
approximate. Visa labels are Zapply's automated estimates and are kept verbatim.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_text

URLS = (
    "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Software-Engineering-Jobs-2027/main/README.md",
    "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md",
)

_SECTION = re.compile(r"<summary>.*?<strong>(.*?)</strong>.*?</summary>", re.I | re.S)
_APPLY = re.compile(r"\]\((https://zapply\.jobs/l/d/([A-Za-z0-9][A-Za-z0-9._-]{0,199}))(?:[/?][^)\s]*)?\)")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")
_AGE = re.compile(r"^(\d{1,4})\s*(mo|m|h|d|w|y)$", re.I)
_INTERN = re.compile(r"\bintern(?:ship)?s?\b|\bco[\s\-\u2010\u2011]?op\b", re.I)
_CLEARANCE = re.compile(
    r"\bTS/SCI\b|\b(?:top )?secret\b|\bpolygraph\b|\bsecurity clearance\b|\bclearance required\b"
    r"|\bU\.?S\.? citizen(?:ship)?\b",
    re.I,
)
_UUID_TAIL = re.compile(r"^(.+)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_NUM_TAIL = re.compile(r"^(.+)-(\d{3,20})$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ATS = {"sr": "smartrecruiters", "oracle": "oracle_hcm"}  # slug prefix -> appa's ATS name


def _clean(cell: str) -> str:
    """A table cell as plain text: no bold markers, markdown escapes or HTML entities."""
    return html.unescape(_MD_ESCAPE.sub(r"\1", cell.replace("**", ""))).strip()


def _posted(age: str, now: datetime) -> date | None:
    """The posting date (UTC) from a relative age ('17m', '21h', '2d', '1mo') or an ISO date; None if unknown."""
    age = age.strip()
    try:
        return date.fromisoformat(age)
    except ValueError:
        pass
    m = _AGE.match(age)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
        "mo": timedelta(days=30 * n),
        "y": timedelta(days=365 * n),
    }[unit]
    return (now - delta).date()


def _ats(slug: str) -> tuple[str, str, str | None]:
    """(ats, job id, direct posting link or None) from an apply slug like 'greenhouse-roblox-8315883'.

    Greenhouse, Lever and Ashby IDs are global and their links follow a fixed pattern, so those get the
    native ID and the ATS link (same key as other sources); other IDs are only unique per tenant, so the
    whole slug is the ID."""
    prefix, _, rest = slug.partition("-")
    prefix = prefix.lower()
    m = (
        (_NUM_TAIL if prefix == "greenhouse" else _UUID_TAIL).match(rest)
        if prefix in ("greenhouse", "lever", "ashby")
        else None
    )
    if m and _TENANT.match(m.group(1)):
        tenant, jid = quote(m.group(1), safe=""), m.group(2)
        link = {
            "greenhouse": f"https://job-boards.greenhouse.io/{tenant}/jobs/{jid}",
            "lever": f"https://jobs.lever.co/{tenant}/{jid}",
            "ashby": f"https://jobs.ashbyhq.com/{tenant}/{jid}",
        }[prefix]
        return prefix, jid, link
    if prefix in ("greenhouse", "lever", "ashby") or not rest:
        return "unknown", slug, None
    return _ATS.get(prefix, prefix), slug, None


def _listing(row: str, category: str, now: datetime) -> Listing | None:
    """A Listing from one table row (internships tagged, not dropped), or None for header and malformed rows."""
    cells = _CELL_SPLIT.split(row.strip())
    if len(cells) < 8:
        return None
    company, title, location, age, visa, apply = (c.strip() for c in cells[1:7])
    m = _APPLY.search(apply)
    if not m:
        return None
    company, title, location = _clean(company), _clean(title), _clean(location)
    if not company or not title:
        return None
    slug = m.group(2)
    ats, jid, direct = _ats(slug)
    zapply = f"https://zapply.jobs/l/d/{quote(slug, safe='')}"
    hints = {"category": category, "level": "new_grad"}
    if _INTERN.search(title):
        hints["level"], hints["employment_type"] = "intern", "internship"
    visa = _clean(visa).replace("\u2705", "").strip()
    if _CLEARANCE.search(title):
        hints["citizenship_required"] = "yes"
    elif visa:
        hints["sponsorship"] = f"{visa} (Zapply estimate)"  # the label verbatim, never interpreted
    if "remote" in f"{title} {location}".lower():
        hints["remote"] = "yes"
    if direct:
        hints["links"] = zapply  # Zapply's redirect, kept for credit and as a fallback
    try:
        return Listing(
            company=company,
            title=title,
            url=direct or zapply,
            location=location,
            posted_at=_posted(age, now),
            ats=ats,
            ats_job_id=jid[:100],
            source_ref=slug,
            hints=hints,
        )
    except ValueError:
        return None


def parse(readme: str, now: datetime | None = None) -> list[Listing]:
    """Every posting in one README, tagged with its section name; ages are relative to now (UTC)."""
    now = now or datetime.now(UTC)
    out: list[Listing] = []
    category = ""
    for line in readme.splitlines():
        sec = _SECTION.search(line)
        if sec:
            category = _clean(re.sub(r"<[^>]+>", "", sec.group(1)))
        elif line.lstrip().startswith("|") and "zapply.jobs/l/" in line:
            x = _listing(line, category, now)
            if x is not None:
                out.append(x)
    return out


def fetch(opts: FeedOptions) -> list[Listing]:
    """Both READMEs' postings (newest first, deduped by apply slug), optionally only since opts.since."""
    seen: set[str] = set()
    out: list[Listing] = []
    for i, url in enumerate(URLS):
        if i:
            time.sleep(0.5)
        text, _ = get_text(url)
        for x in parse(text):
            if x.source_ref not in seen:
                seen.add(x.source_ref)
                out.append(x)
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="zapply_newgrad2027",
        description="Zapply New-Grad 2027 lists (GitHub): recent new-grad SWE, AI/ML, security, systems roles; "
        "~10-min updates",
        fetch=fetch,
        terms="Data by Zapply (github.com/zapplyjobs). Credit Zapply and link the repo; do not resubmit the listings "
        "to other job aggregators or feeds. Visa/degree fields are Zapply's automated estimates.",
    )
)
