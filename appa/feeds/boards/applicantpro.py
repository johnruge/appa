"""ApplicantPro career sites (isolved Talent Acquisition), https://{slug}.applicantpro.com/jobs/.

The public jobs page embeds the site's numeric `domain_id`; the page's own Vue widget then loads every
open posting in one JSON request, `/core/jobs/{domain_id}?getParams={}` (no pagination, no login, no key;
without getParams the server returns a PHP error page). Rows carry title, city/state/country, posted date,
department, workplace type, employment type and pay range, but no text: describe() reads the posting
page's schema.org JobPosting JSON-LD (one request) for postings that pass appa's free filters.
robots.txt disallows only /admin, /stats and the internal-job pages. Old
www.applicantpro.com/openings/{slug}/jobs/{id}/... links redirect to the tenant site, so both are boards.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

DOMAIN = "applicantpro.com"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under applicantpro.com
_ID = re.compile(r"^[0-9]{1,12}$")
_DOMAIN_ID = re.compile(r"domain_id = '(\d{1,12})'")
_TITLE = re.compile(r"<title>\s*Job Listings - (.{1,300}?) Jobs\s*</title>", re.S)
_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_POSTING = re.compile(r"^/jobs/([0-9]{1,12})(?:\.html)?(?:/|$)")
_OPENINGS = re.compile(r"^/openings/([A-Za-z0-9][A-Za-z0-9-]{0,62})/jobs(?:/([0-9]{1,12}))?(?:/|$)")
_NOT_BOARDS = {
    "www",
    "feeds",
    "admin",
    "api",
    "app",
    "apps",
    "help",
    "support",
    "login",
    "my",
    "secure",
    "static",
    "cdn",
    "images",
    "mail",
    "status",
    "blog",
    "dev",
    "staging",
}
_EMPLOYMENT = {
    "full time": "full_time",
    "part time": "part_time",
    "full or part time": "full_time, part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "seasonal": "temporary",
}
_REMOTE = {"fully remote": "yes", "remote": "yes", "hybrid": "hybrid", "on-site": "no", "onsite": "no"}
MAX_DESCRIPTION = 100_000


def _slug(slug: str) -> str:
    """The validated, lowercased tenant slug; ValueError for anything that isn't one plain subdomain label."""
    if not isinstance(slug, str) or not _SLUG.match(slug) or slug.lower() in _NOT_BOARDS:
        raise ValueError(f"not an ApplicantPro board slug: {slug!r}")
    return slug.lower()


def _base(slug: str) -> str:
    """The career site root for a validated slug."""
    return f"https://{quote(_slug(slug), safe='')}.{DOMAIN}"


def _posted(raw) -> date | None:
    """The posting date from 'Jul 28, 2026', or None."""
    try:
        return datetime.strptime(str(raw or "").strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def _text(raw) -> str:
    """A stripped string, '' for None and non-strings."""
    return raw.strip() if isinstance(raw, str) else ""


def _location(row: dict) -> str:
    """'City, ST' (plus the country when it isn't the US), else the row's free-text job location."""
    parts = [p for p in (_text(row.get("city")), _text(row.get("abbreviation")) or _text(row.get("stateName"))) if p]
    country = _text(row.get("iso3"))
    if parts and country and country != "USA":
        parts.append(country)
    return ", ".join(parts) or _text(row.get("jobLocation")) or country


def _salary(row: dict) -> str:
    """The posted pay range as shown ('70,000 - 90,000 per year'), or ''."""
    lo, hi = _text(row.get("minSalary")), _text(row.get("maxSalary"))
    if not (lo or hi) or (lo in ("0", "0.00") and hi in ("", "0", "0.00")):
        return ""
    pay = lo if not hi or lo == hi else (hi if not lo else f"{lo} - {hi}")
    frame, kind = _text(row.get("payTypeFrame")), _text(row.get("payType"))
    return " ".join(p for p in (pay, frame, f"({kind})" if kind else "") if p)


def _hints(row: dict) -> dict[str, str]:
    """Facts the row states: workplace type, employment type, department, country, pay."""
    hints: dict[str, str] = {}
    work = _text(row.get("workplaceType")).lower()
    if work in _REMOTE:
        hints["remote"] = _REMOTE[work]
    kind = _text(row.get("employmentType"))
    if kind:
        hints["employment_type"] = _EMPLOYMENT.get(kind.lower(), kind.lower().replace(" ", "_"))
        if kind.lower() in ("intern", "internship"):
            hints["level"] = "intern"
    category = _text(row.get("classification")) or _text(row.get("orgTitle"))
    if category and category.lower() != "other":
        hints["category"] = category
    if _text(row.get("iso3")):
        hints["country"] = _text(row.get("iso3"))
    if _salary(row):
        hints["salary"] = _salary(row)
    return hints


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's ApplicantPro site (no descriptions; see describe())."""
    opts = opts or FeedOptions()
    slug = _slug(slug)
    page, _ = get_text(f"{_base(slug)}/jobs/")
    m = _DOMAIN_ID.search(page)
    if not m:  # unknown tenants redirect to a "not set" page
        return []
    t = _TITLE.search(page)
    company = html.unescape(t.group(1)).strip() if t else slug
    data = get_json(f"{_base(slug)}/core/jobs/{quote(m.group(1), safe='')}", params={"getParams": "{}"})
    body = data.get("data") if isinstance(data, dict) and data.get("success") else None
    rows = body.get("jobs") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Listing] = []
    seen: set[str] = set()
    for row in rows:
        if len(out) >= opts.max_results:
            break
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "")
        if not _ID.match(job_id) or job_id in seen:
            continue
        posted = _posted(row.get("startDateRef"))
        if opts.since and posted and posted < opts.since:
            continue
        try:
            out.append(
                Listing(
                    company=company,
                    title=str(row.get("title") or ""),
                    url=f"https://{slug}.{DOMAIN}/jobs/{job_id}",
                    location=_location(row),
                    posted_at=posted,
                    ats="applicantpro",
                    ats_job_id=job_id,
                    source_ref=f"{slug}:{job_id}",
                    hints=_hints(row),
                )
            )
            seen.add(job_id)
        except ValueError:
            continue
    return out


def _posting(url: str) -> tuple[str, str] | None:
    """(slug, posting id) of an ApplicantPro posting link, if it is one."""
    slug = board_of(url)
    if not slug:
        return None
    path = urlparse(url.strip()).path
    m = _OPENINGS.match(path)
    job_id = m.group(2) if m else (_POSTING.match(path).group(1) if _POSTING.match(path) else None)
    return (slug, job_id) if job_id else None


def _job_posting(page: str) -> dict | None:
    """The schema.org JobPosting object in a posting page's JSON-LD, if any."""
    for block in _LD_JSON.findall(page):
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def describe(listing: Listing) -> str:
    """The posting's full text, from the JobPosting JSON-LD on its page."""
    ref = _posting(listing.url)
    if not ref:
        return ""
    slug, job_id = ref
    page, _ = get_text(f"{_base(slug)}/jobs/{quote(job_id, safe='')}")
    job = _job_posting(page)
    if not job:
        return ""
    text, hidden = html_to_text(str(job.get("description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESCRIPTION]


def board_of(url: str) -> str | None:
    """The tenant slug of an ApplicantPro jobs link ({slug}.applicantpro.com/jobs/... or
    www.applicantpro.com/openings/{slug}/jobs/...), else None."""
    try:
        p = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if host in (DOMAIN, f"www.{DOMAIN}"):
        m = _OPENINGS.match(p.path)
        label = m.group(1).lower() if m else ""
    elif host.endswith(f".{DOMAIN}"):
        label = host[: -len(f".{DOMAIN}")]
        if not (p.path.lower() == "/jobs" or p.path.lower().startswith("/jobs/")):
            return None
    else:
        return None
    if not label or "." in label or not _SLUG.match(label) or label in _NOT_BOARDS:
        return None
    return label


BOARD = register_board(
    BoardFeed(
        ats="applicantpro",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="karmak",
        terms="Public career-site JSON (no login); robots.txt disallows only /admin, /stats and internal-job "
        "pages; isolved's terms permit job seekers' personal use. Two requests per board (jobs page for the "
        "domain id, then the jobs JSON); describe() is one page request per posting.",
        hosts=(DOMAIN,),
    )
)
