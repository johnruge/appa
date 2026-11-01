"""ResuMax New-Grad Tech Jobs: a daily-regenerated GitHub list (resumax/new-grad-tech-jobs, MIT license) of
new-grad software, data/AI, security, DevOps, mobile and hardware roles posted in the last 30 days. There is
no JSON, only a README with one Markdown pipe table per category, each cut to its newest ~150 rows (the
rest live on resumax.ai, which appa doesn't scrape). The Apply links go straight to the ATS (SmartRecruiters,
Ashby, Workable, Greenhouse, Lever...), so the list's main value is discovering company boards. The list is
global; with opts.us_only appa keeps rows whose location reads as the US, a US state/city, plain Remote,
or worldwide remote ("Global Remote").
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from appa.core.jobid import job_key
from appa.feeds import KNOWN_ATS, FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_text

URL = "https://raw.githubusercontent.com/resumax/new-grad-tech-jobs/main/README.md"
SECTIONS = (
    "Software Engineering",
    "Data, AI & ML",
    "Security",
    "DevOps, Infra & Cloud",
    "Mobile",
    "Hardware & Embedded",
)
HEADER = ["company", "role", "location", "apply", "tailor", "age"]

_PIPE = re.compile(r"(?<!\\)\|")
_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")
_COMPANY = re.compile(r"^\*\*\[(.+?)\]\([^)]*\)\*\*")
_APPLY = re.compile(r"\[Apply\]\((https?://[^)\s]+)\)")
_ROLE = re.compile(r"[?&]role=([A-Za-z0-9-]{1,200})")
_AGE = re.compile(r"^(\d{1,4})([hdw])$")
_WORKABLE = re.compile(r"^https://apply\.workable\.com/(?:[^/]+/)?j/([A-Za-z0-9]{4,40})(?:/|$)")

_US_WORD = re.compile(r"(?<![A-Za-z])(u\.s\.(a\.)?|u\.s\.a)(?![A-Za-z])|\b(us|usa|united states|amer|americas)\b", re.I)
_WORLDWIDE = re.compile(r"\b(global|worldwide|anywhere|international)\b", re.I)
_NON_US_CITIES = re.compile(r"\b(toronto|vancouver|montreal|montr\u00e9al)\b", re.I)
_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
_STATE_NAMES = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|district of columbia|"
    r"florida|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    r"south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)\b",
    re.I,
)
_US_CITIES = re.compile(
    r"\b(san francisco|bay area|nyc|seattle|san jose|san mateo|palo alto|mountain view|sunnyvale|menlo park|"
    r"los angeles|san diego|boston|chicago|austin|denver|atlanta|scotts valley|oklahoma city|pittsburgh|"
    r"philadelphia|miami|dallas|houston|portland|salt lake city|redmond|san carlos)\b",
    re.I,
)
_REMOTE_ONLY = re.compile(r"^[\W_]*(fully\s+)?remote([\W_]+(first|friendly|only|hybrid))*[\W_]*$", re.I)


def _plain(cell: str) -> str:
    """A table cell as plain text (Markdown escapes undone, whitespace collapsed)."""
    return " ".join(_ESC.sub(r"\1", cell).split())


def _cells(line: str) -> list[str]:
    """The cells of one Markdown pipe-table row (escaped pipes stay inside their cell)."""
    return [c.strip() for c in _PIPE.split(line.strip().strip("|"))]


def _is_us_part(part: str) -> bool:
    """Whether one location (e.g. 'Austin, TX', 'Paris, IDF, fr', 'Remote') reads as in the US."""
    bits = [b.strip() for b in part.split(",") if b.strip()]
    if not bits:
        return False
    last = bits[-1]
    if re.fullmatch(r"[a-z]{2}", last):  # the list's lowercase ISO country suffix ("us", "fr", "ca")
        return last == "us"
    if _US_WORD.search(part) or _STATE_NAMES.search(part) or _US_CITIES.search(part):
        return True
    if re.search(r"\bremote\b", part, re.I) and _WORLDWIDE.search(part):
        return True  # worldwide remote is usually open to US candidates
    if _NON_US_CITIES.search(part):
        return False  # "Toronto, CA" is Canada, not California
    if any(b in _STATES for b in bits[1:]):  # "Austin, TX" (uppercase state code after a comma)
        return True
    return bool(_REMOTE_ONLY.match(part))


def is_us(location: str) -> bool:
    """Whether any of a row's locations (split on ';', '|', '•', ' or ') is in the US or plain Remote."""
    if not location.strip():
        return True  # no location given: keep it, the screener decides
    parts = re.split(r"\s*(?:;|\||•|\bor\b)\s*", location)
    return any(_is_us_part(p) for p in parts if p.strip())


def _tables(text: str):
    """(section, row cells) for every data row under the six category headings."""
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            section = name if name in SECTIONS else None
        elif section and line.startswith("| **"):
            yield section, _cells(line)


def _listing(section: str, row: list[str], today: date) -> Listing | None:
    """A Listing from one table row, or None when the row is malformed."""
    if len(row) != len(HEADER):
        return None
    cell = dict(zip(HEADER, row, strict=False))
    name, apply = _COMPANY.match(cell["company"]), _APPLY.search(cell["apply"])
    if not (name and apply):
        return None
    url = apply.group(1).strip()
    company, title, location = _plain(name.group(1)), _plain(cell["role"]), _plain(cell["location"])
    key = job_key(url)
    scope, _, job_id = key.rpartition(":") if key else ("", "", "")
    if not job_id and (w := _WORKABLE.match(url)):
        job_id = w.group(1)
    age = _AGE.match(_plain(cell["age"]))
    days = 0 if not age else int(age.group(1)) * {"h": 0, "d": 1, "w": 7}[age.group(2)]
    role = _ROLE.search(cell["tailor"])
    hints = {"level": "new_grad", "category": section}
    if "🌐" in cell["company"] or location.lower().startswith("remote"):
        hints["remote"] = "yes"
    if re.search(r"citizenship|clearance", title, re.I):
        hints["citizenship_required"] = "yes"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=location[:500],
            posted_at=today - timedelta(days=days) if age else None,
            ats=scope if scope in KNOWN_ATS else "unknown",
            ats_job_id=job_id[:100] or None,
            source_ref=role.group(1) if role else None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every row of the README (newest first): US/remote only when opts.us_only, since opts.since if set."""
    text, _ = get_text(URL)
    if "| Company | Role |" not in text:
        raise ValueError("ResuMax README has no job table")
    today, out, seen = date.today(), [], set()
    for section, row in _tables(text):
        x = _listing(section, row, today)
        if x is None:
            continue
        keys = {x.url} | (
            {(x.ats if x.ats != "unknown" else urlsplit(x.url).hostname, x.ats_job_id)} if x.ats_job_id else set()
        )
        if keys & seen:
            continue
        if opts.us_only and not is_us(x.location):
            continue
        if opts.since and x.posted_at and x.posted_at < opts.since:
            continue
        seen |= keys
        out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="resumax_newgrad_tech",
        description="ResuMax new-grad-tech-jobs (GitHub): global new-grad SWE/data/security/infra/hardware roles, "
        "last 30 days, direct ATS links",
        fetch=fetch,
        terms="Public GitHub repository, MIT license; one raw README fetch per run. The README shows only the "
        "newest ~150 rows per category; the full list is on resumax.ai (not scraped).",
    )
)
