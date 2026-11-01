"""coconight01/2027-North-America-New-Grad-Jobs: an automated GitHub list (updated daily) that merges
many other new-grad repositories (speedyapply, jobright, zapply, SimplifyJobs...) plus manually
verified official postings into one ~3k-row file, data/jobs.json, read from raw.githubusercontent.com.

Rows carry the role, a '; '-separated location, category, sponsorship / citizenship labels (mostly
'Unknown'), annual salary bounds and a new-grad confidence. Fields specific to the repo owner
(priority, personalized_*, match, application_*) are ignored. Only rows with status 'Open' are kept.
Note the list already drops roles that say "no sponsorship" or "U.S. citizenship / clearance required".

Links are mostly ATS pages, but some are aggregators (jobright.ai, zapply.jobs, indeed). Zapply links
redirect to Zapply's generic jobs page, so slugs with a fixed pattern become the direct posting link
(Greenhouse, Lever, Ashby, SmartRecruiters, Gem, ByteDance, Google; the zapply link is kept in
hints['links']). Workday, Oracle and Amazon slugs can't be turned back into a posting link (the host
and site are lost), so those rows are dropped rather than pointing at the generic page; the
zapply_newgrad2027 feed lists the same zapply postings. Otherwise the dedupe ID is the ATS ID in the
link when appa recognizes one, else the Workable shortcode, else a trailing requisition number in the
role, e.g. "Software Engineer (6865704002)", which is cut from the title.

Roles whose title says intern/co-op/summer analyst get level 'intern'; roles from a zapply internships
list, or titled "Summer" without saying new grad, get no level (the list's new-grad confidence is unreliable for them).
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, quote, urlparse

from appa.core.ats_urls import parse_url
from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json

URL = "https://raw.githubusercontent.com/coconight01/2027-North-America-New-Grad-Jobs/main/data/jobs.json"
_REQ_ID = re.compile(r"\s*\((\d{5,20})\)\s*$")
_WORKABLE = re.compile(r"^/[\w.-]+/j/([A-Za-z0-9]{6,20})(?:/|$)")
_ZAPPLY = re.compile(r"^/l/d/([a-z0-9][a-z0-9._-]{0,99})/?$", re.I)
_UNKNOWN = {"", "unknown", "not listed", "none", "n/a"}
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NUM_TAIL = re.compile(r"^(.+)-(\d{3,20})$")
_UUID_TAIL = re.compile(r"^(.+)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_GEM_TAIL = re.compile(r"^(.+?)-(am9icG9zdD[A-Za-z0-9_-]{4,90})$")  # Gem IDs are base64 'jobpost:...'
_DIGITS = re.compile(r"^\d{6,25}$")
_INTERN = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b|\bsummer (?:analyst|associate|intern)", re.I)
_SUMMER = re.compile(r"\bsummer\b", re.I)
_NEW_GRAD = re.compile(r"\bnew (?:college )?grad|\bgraduate\b|\bearly career\b|\bentry[- ]level\b", re.I)
_LINKS = {
    "greenhouse": (_NUM_TAIL, "https://job-boards.greenhouse.io/{}/jobs/{}"),
    "lever": (_UUID_TAIL, "https://jobs.lever.co/{}/{}"),
    "ashby": (_UUID_TAIL, "https://jobs.ashbyhq.com/{}/{}"),
    "sr": (_NUM_TAIL, "https://jobs.smartrecruiters.com/{}/{}"),
    "gem": (_GEM_TAIL, "https://jobs.gem.com/{}/{}"),
}
_ATS = {"sr": "smartrecruiters"}


def _known(v) -> str:
    """The field as text, or "" when the list marks it unknown."""
    s = " ".join(str(v if v is not None else "").split())
    return "" if s.lower() in _UNKNOWN else s


def _date(v) -> date | None:
    """A YYYY-MM-DD string as a date (None if missing or invalid)."""
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _money(v) -> int | None:
    """An annual salary bound as an int (None if missing or not a positive number)."""
    try:
        n = int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _salary(row: dict) -> str:
    """The pay range from salary_min/max_annual, else the list's salary text."""
    lo, hi = _money(row.get("salary_min_annual")), _money(row.get("salary_max_annual"))
    if lo and hi and hi != lo:
        return f"${lo:,}-${hi:,}/yr"
    if lo or hi:
        return f"${lo or hi:,}/yr"
    return _known(row.get("salary"))


def _zapply(slug: str) -> tuple[str, str, str] | None:
    """(ats, job id, direct posting link) for a zapply slug like 'greenhouse-roblox-8315883', or None when
    the slug doesn't map to a posting link. Greenhouse/Lever/Ashby keep the native ID (same key as other
    sources); per-tenant IDs (SmartRecruiters, Gem) use the whole slug."""
    prefix, _, rest = slug.partition("-")
    prefix = prefix.lower()
    if prefix in ("bytedance", "google") and _DIGITS.match(rest):
        link = {
            "bytedance": "https://jobs.bytedance.com/en/position/{}/detail",
            "google": "https://www.google.com/about/careers/applications/jobs/results/{}",
        }[prefix]
        return prefix, rest, link.format(rest)
    pattern = _LINKS.get(prefix)
    m = pattern[0].match(rest) if pattern else None
    if not m or not _PART.match(m.group(1)) or not _PART.match(m.group(2)):
        return None
    link = pattern[1].format(quote(m.group(1), safe=""), quote(m.group(2), safe=""))
    jid = m.group(2) if prefix in ("greenhouse", "lever", "ashby") else slug
    return _ATS.get(prefix, prefix), jid, link


def _ids(url: str, role: str) -> tuple[str, str | None, str, str | None] | None:
    """(ats, posting id, title, direct link or None) for a row: the link's ATS ID, else the zapply slug's
    direct link, else the Workable shortcode, else a trailing req ID. None for zapply links that lead nowhere."""
    m = _REQ_ID.search(role)
    title = role[: m.start()] if m and m.start() > 0 else role
    ref = parse_url(url)
    if ref and ref.job_id:
        return ref.ats, ref.job_id[:100], title, None
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host == "zapply.jobs" or host.endswith(".zapply.jobs"):
        z = _ZAPPLY.match(p.path)
        hit = _zapply(z.group(1)) if z else None
        if not hit:
            return None
        return hit[0], hit[1][:100], title, hit[2]
    if host == "apply.workable.com":
        w = _WORKABLE.match(p.path)
        if w:
            return "workable", w.group(1).lower(), title, None
    key = job_key(url)
    if key:
        return ("workday" if "myworkdayjobs.com:" in key else "unknown"), key.split(":", 1)[1][:100], title, None
    return "unknown", m.group(1) if m else None, title, None


def _listing(row) -> Listing | None:
    """A Listing from one row of the file, or None for rows that aren't open or are malformed."""
    if not isinstance(row, dict) or str(row.get("status") or "").strip().lower() != "open":
        return None
    url, role = str(row.get("url") or "").strip(), " ".join(str(row.get("role") or "").split())
    if not url.lower().startswith(("http://", "https://")) or not role:
        return None
    ids = _ids(url, role)
    if ids is None:
        return None
    ats, jid, title, direct = ids
    location = _known(row.get("location"))
    sponsorship, citizenship = _known(row.get("sponsorship")), _known(row.get("citizenship_required"))
    degree = _known(row.get("degree"))
    if not degree and _known(row.get("phd_required")).lower() == "yes":
        degree = "PhD"
    hints = {
        "category": _known(row.get("category")),
        "country": _known(row.get("country")),
        "degrees": degree,
        "salary": _salary(row),
        "employment_type": "full_time",
    }
    from_interns = any("internship" in v for v in parse_qs(urlparse(url).query).get("s", []))
    if _INTERN.search(title):
        hints["level"], hints["employment_type"] = "intern", "internship"
    elif from_interns or (_SUMMER.search(title) and not _NEW_GRAD.search(title)):
        hints.pop("employment_type")
    elif _known(row.get("ng_confidence")).lower() in ("confirmed", "likely"):
        hints["level"] = "new_grad"
    if sponsorship:
        hints["sponsorship"] = sponsorship
    if citizenship.lower() in ("yes", "true", "required"):
        hints["citizenship_required"] = "yes"
    if "remote" in location.lower():
        hints["remote"] = "yes"
    if direct:
        hints["links"] = url  # the zapply redirect, kept for credit and as a fallback
    try:
        return Listing(
            company=str(row.get("company") or ""),
            title=title,
            url=direct or url,
            location=location[:500],
            posted_at=_date(row.get("posted_date")) or _date(row.get("date_added")),
            ats=ats,
            ats_job_id=jid,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every open listing (newest first), optionally only those posted since opts.since."""
    rows = get_json(URL)
    if not isinstance(rows, list):
        raise ValueError("coconight jobs.json is not a list")
    out = [x for x in map(_listing, rows) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="coconight_na_newgrad",
        description="coconight01 2027 North America New Grad Jobs (GitHub): ~3k merged new-grad roles, daily",
        fetch=fetch,
        terms="Public GitHub repository with no license; personal use only, don't redistribute the data.",
    )
)
