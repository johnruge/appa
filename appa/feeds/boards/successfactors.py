"""SAP SuccessFactors Recruiting boards, through the legacy career site's public XML job feed (no key).

GET https://{careerHost}/career?company={companyId}&career_ns=job_listing_summary&resultType=XML returns every
job posted to a company's external career site in one response (SAP KBA 2428902 documents it for job-board
syndication): <Job-Listing> holding one <Job> per posting. A <Job>'s children are admin-configured, so they
are read generically: JobTitle, Job-Description (HTML in CDATA), usually ReqId, sometimes Posted-Date,
Location, Department, and custom fields as <mfieldN>/<filterN> with <label>/<value> children (e.g. "Posting
Country"). Unnamed custom fields come out as `<>116</>`, which isn't XML, so those are dropped before parsing.
Titles are often HTML-escaped inside the CDATA ("IBP &amp; PP/DS").

careerHost is career{N}.successfactors.com/.eu or career{N}.sapsf.com/.eu (the data center), taken from the
discovered link; companyId is the link's `company=` parameter. The slug is "{careerHost}:{companyId}". An
unknown company returns an HTML page (raised as an error); a company that moved to Recruiting Marketing (RMK,
jobs.{company}.com) gets an empty <Job-Listing/>: SAP deprecated this feed for RMK customers. The feed has no
company name, so Listing.company is the opaque companyId (e.g. "C0002633795P"): a known limitation. Most
tenants configure no Posted-Date, so posted_at is None and opts.since only filters on tenants that do.

Opt-in: robots.txt on the career hosts says "Disallow: /*company", which matches this feed URL, so nothing is
fetched unless the owner has approved it. Set APPA_SUCCESSFACTORS_BOARDS to a comma-separated list of approved
slugs (or "all"); otherwise fetch raises RuntimeError before any request. Approved boards get one request each.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import date, datetime
from urllib.parse import parse_qs, quote, urlparse

from defusedxml import ElementTree

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_HOST = re.compile(r"^career\d{1,4}\.(?:successfactors|sapsf)\.(?:com|eu)$")
_COMPANY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REQ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_JUNK = re.compile(rb"<!\[CDATA\[.*?\]\]>|<>[^<]*</>", re.S)  # keep CDATA; drop nameless <>x</> fields
_DATES = ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%b %d, %Y", "%d-%b-%Y")
_TYPES = {
    "full time": "full_time",
    "full-time": "full_time",
    "regular": "full_time",
    "part time": "part_time",
    "part-time": "part_time",
    "contract": "contract",
    "temporary": "contract",
    "intern": "internship",
    "internship": "internship",
}
MAX_DESC = 100_000
OPT_IN_ENV = "APPA_SUCCESSFACTORS_BOARDS"
_PATHS = ("/career", "/careers", "/sfcareer/jobreqcareer", "/sfcareer/jobreqcareerpvt")


def _check(slug: str) -> tuple[str, str]:
    """(careerHost, companyId) of a "host:company" slug, if both parts are safe; else ValueError."""
    host, sep, company = slug.partition(":") if isinstance(slug, str) else ("", "", "")
    host = host.lower()
    if not sep or not _HOST.fullmatch(host) or not _COMPANY.fullmatch(company):
        raise ValueError(f"bad successfactors slug: {slug!r}")
    return host, company


def feed_url(slug: str) -> str:
    """The XML job feed URL for a board."""
    host, company = _check(slug)
    return f"https://{host}/career?company={quote(company, safe='')}&career_ns=job_listing_summary&resultType=XML"


def job_url(host: str, company: str, req_id: str) -> str:
    """The posting page on the legacy career site."""
    return (
        f"https://{host}/career?career_ns=job_listing&company={quote(company, safe='')}"
        f"&career_job_req_id={quote(req_id, safe='')}"
    )


def _approved(slug: str) -> bool:
    """Whether the owner opted this board in through APPA_SUCCESSFACTORS_BOARDS (a slug list, or "all")."""
    allowed = {s.strip().lower() for s in os.environ.get(OPT_IN_ENV, "").split(",") if s.strip()}
    return "all" in allowed or slug.lower() in allowed


def _key(tag: str) -> str:
    """A child tag folded for matching: 'Posted-Date' -> 'posteddate'."""
    return re.sub(r"[^a-z0-9]", "", tag.lower())


def _fields(job) -> dict[str, str]:
    """Every child of a <Job> as {folded name: text}; label/value custom fields go under their label."""
    out: dict[str, str] = {}
    for c in job:
        label = c.find("label")
        if label is not None:
            name, value = label.text or "", c.findtext("value") or ""
        else:
            name, value = c.tag, c.text or ""
        name, value = _key(name), value.strip()
        if name and value and name not in out:
            out[name] = value
    return out


def _first(f: dict[str, str], *names: str) -> str:
    """The first non-empty field among these folded names, HTML entities decoded."""
    return next((" ".join(html.unescape(f[n]).split()) for n in names if f.get(n)), "")


def _day(raw: str) -> date | None:
    """A posted date in one of the formats tenants use, or None."""
    for fmt in _DATES:
        try:
            return datetime.strptime(raw.strip()[:20], fmt).date()
        except ValueError:
            continue
    return None


def _description(raw: str) -> str:
    """The job's HTML description as text, hidden text flagged."""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(job, host: str, company: str) -> Listing:
    """One <Job> as a Listing (ValueError when the title or requisition ID is missing or bad)."""
    f = _fields(job)
    req = _first(f, "reqid", "jobreqid", "requisitionid")
    if not _REQ.fullmatch(req):
        raise ValueError(f"bad successfactors req id: {req[:30]!r}")
    country = _first(f, "postingcountry", "country")
    location = _first(f, "location", "joblocation", "city") or country.replace("|", ", ")
    etype = _first(f, "employmenttype", "jobtype", "employeetype").lower()
    hints = {
        "country": country.replace("|", ", "),
        "category": _first(f, "department", "category", "jobcategory"),
        "employment_type": _TYPES.get(etype, ""),
        "remote": "yes" if "remote" in location.lower() else "",
    }
    return Listing(
        company=company,
        title=_first(f, "jobtitle", "title"),
        url=job_url(host, company, req),
        location=location[:500],
        posted_at=_day(_first(f, "posteddate", "postingdate", "postdate")),
        description=_description(f.get("jobdescription", "")),
        ats="successfactors",
        ats_job_id=f"{company}:{req}"[:100],
        source_ref=req,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every job on this company's SuccessFactors career site, with descriptions (one request, no paging)."""
    opts = opts or FeedOptions()
    host, company = _check(slug)
    if not _approved(f"{host}:{company}"):
        raise RuntimeError(
            f"{OPT_IN_ENV} is not set for {host}:{company} (robots.txt disallows this feed; "
            "add the slug once the owner approves)"
        )
    body = _JUNK.sub(lambda m: m[0] if m[0].startswith(b"<!") else b"", get_bytes(feed_url(slug)))
    if b"<Job-Listing" not in body[:500]:
        raise RuntimeError(f"successfactors board {slug} returned no job feed (unknown company?)")
    root = ElementTree.fromstring(body)
    out, seen = [], set()
    for job in root.findall("Job"):
        try:
            x = _listing(job, host, company)
        except ValueError as e:
            log.debug("skipping successfactors job on %s: %s", slug, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The "host:company" slug of a career{N}.successfactors/sapsf link (/career, /sfcareer/jobreqcareer), or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
        company = parse_qs(p.query).get("company", [""])[0]
    except (AttributeError, ValueError):
        return None
    if not _HOST.fullmatch(host) or p.path.rstrip("/").lower() not in _PATHS or not company:
        return None
    try:
        _check(f"{host}:{company}")
    except ValueError:
        return None
    return f"{host}:{company}"


BOARD = register_board(
    BoardFeed(
        ats="successfactors",
        fetch=fetch,
        board_of=board_of,
        example="career4.successfactors.com:C0002633795P",
        terms="Public XML job feed SAP documents for job-board syndication (KBA 2428902; no key), but robots.txt on "
        "the career hosts disallows it (Disallow: /*company). Opt-in only: fetches just the boards listed in "
        "APPA_SUCCESSFACTORS_BOARDS (or 'all'), one request per board, about daily. Deprecated for RMK tenants "
        "(often empty). company is the tenant ID; most tenants have no posted dates.",
        hosts=(
            "career2.successfactors.eu",
            "career4.successfactors.com",
            "career5.successfactors.eu",
            "career8.successfactors.com",
            "career10.successfactors.com",
        ),
    )
)
