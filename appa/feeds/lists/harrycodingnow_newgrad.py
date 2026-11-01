"""harrycodingnow/new-grad-2027-tracker: a GitHub repo whose bot polls company career sites (Greenhouse,
Workday, Eightfold, Amazon/Stripe/... JSON APIs) every few hours for 2027 new-grad SWE / AI / full-stack
roles in the US, and publishes the currently open ones as data/active_jobs.json (a few hundred rows, each
with the full posting text and a direct ATS apply link). appa reads that one file.

The bot also marks some rows "disqualified" (its sponsorship screen read the posting as citizenship or
clearance only). Those rows are kept: the screen misfires (its "itar" flag matches EEO text like "military
status"), and appa's own filter shows rejected listings with a reason. Only the bot's explicit "must be a u.s.
citizen", "green card holder" and "ts/sci" flags become citizenship_required; its sponsorship label is kept
verbatim as a hint.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlsplit

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

URL = "https://raw.githubusercontent.com/harrycodingnow/new-grad-2027-tracker/main/data/active_jobs.json"
MAX_DESCRIPTION = 100_000
_ATS = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "workday": "workday",
    "eightfold": "eightfold",
    "smartrecruiters": "smartrecruiters",
}
_REMOTE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no", "on-site": "no"}
_CITIZEN_FLAGS = ("must be a u.s. citizen", "green card holder", "ts/sci")


def _date(s) -> date | None:
    """A YYYY-MM-DD date from the file (None if missing or invalid)."""
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _text(raw) -> str:
    """Posting text, with any HTML converted and hidden text flagged, capped in size."""
    raw = str(raw or "")
    if "<" in raw:
        raw = mark_hidden(*html_to_text(raw))
    return raw[:MAX_DESCRIPTION]


def _employment(raw: str) -> str:
    """The file's free-form employment type mapped to HINT_KEYS vocabulary ('' if unknown)."""
    v = raw.lower().replace("-", " ").replace("_", " ")
    if "intern" in v:
        return "internship"
    if "part time" in v:
        return "part_time"
    if "contract" in v or "temporary" in v:
        return "contract"
    if "full time" in v or "permanent" in v or v == "regular":
        return "full_time"
    return ""


def _listing(row: dict) -> Listing | None:
    """A Listing from one row of the file, or None for inactive or malformed rows."""
    if not isinstance(row, dict) or row.get("status", "active") != "active":
        return None
    url = str(row.get("application_url") or row.get("source_url") or "")
    company, title = str(row.get("company") or ""), str(row.get("title") or "")
    if not (url.startswith(("http://", "https://")) and company.strip() and title.strip()):
        return None
    employment = _employment(str(row.get("employment_type") or ""))
    hints = {
        "country": str(row.get("country") or ""),
        "remote": _REMOTE.get(str(row.get("workplace_type") or "").lower(), ""),
        "category": str(row.get("department") or ""),
        "employment_type": employment,
    }
    if employment == "internship":
        hints["level"] = "intern"
    sponsorship = str(row.get("sponsorship_classification") or "")
    if sponsorship and sponsorship != "unclear":
        hints["sponsorship"] = sponsorship
    flags = " ".join(str(f) for f in row.get("international_student_risk_flags") or []).lower()
    if any(f in flags for f in _CITIZEN_FLAGS):
        hints["citizenship_required"] = "yes"
    source_url = str(row.get("source_url") or "")
    if source_url.startswith(("http://", "https://")) and source_url != url:
        hints["links"] = source_url
    job_id = str(row.get("source_job_id") or "").strip()[:100] or None
    ats = _ATS.get(str(row.get("source_type") or ""), "unknown")
    if ats == "unknown" and "gh_jid" in parse_qs(urlsplit(url).query):
        ats = "greenhouse"
    try:
        return Listing(
            company=company,
            title=title,
            url=url,
            location=str(row.get("location") or "")[:500],
            posted_at=_date(row.get("date_posted")),
            description=_text(row.get("description")),
            ats=ats,
            ats_job_id=job_id,
            source_ref=str(row.get("job_id") or "")[:300] or None,
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every open posting (newest first), optionally only those posted since opts.since."""
    data = get_json(URL)
    rows = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("harrycodingnow active_jobs.json has no jobs list")
    out, seen = [], set()
    for x in map(_listing, rows):
        if x is None or x.url in seen:
            continue
        seen.add(x.url)
        out.append(x)
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="harrycodingnow_newgrad",
        description="harrycodingnow new-grad-2027-tracker (GitHub): bot-scraped 2027 new-grad SWE/AI roles in the US, "
        "full text + direct ATS links, updated every few hours",
        fetch=fetch,
        terms="Public GitHub repository with no license file; personal use only, don't redistribute the data. "
        "Its sponsorship labels are automated and not authoritative.",
    )
)
