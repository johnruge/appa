"""Phenom People career sites (e.g. careers.mastercard.com, careers.adobe.com, jobs.cvshealth.com), through the
public JSON search the site's own search page calls: POST https://{domain}/widgets with ddoKey "refineSearch".
No login or key; robots.txt (Mastercard's, typical of Phenom) disallows only */apply, */jobcart, */chatbot and
tracking scripts.

A board slug is the career domain, plus its locale path when it has one, joined with ":"
(careers.mastercard.com:us:en, jobs.thermofisher.com:global:en, or just careers.kohls.com). fetch reads the
site's search page once for its tenant code (refNum), locale and company name, then runs one search per
opts.queries (50 rows a page, from += 50 until totalHits), filtered to the United States with opts.us_only (if a
tenant spells the country differently and the filter finds nothing, that query runs unfiltered). Listing links
are https://{domain}/{cc}/{lang}/job/{jobId}. Many Phenom sites front Workday: the row's applyUrl (without its
/apply suffix) goes in hints["links"] so the Workday copy of the posting can be matched and its board found.
Search rows carry only an AI teaser, so description is empty and describe() reads the posting page's embedded
job record (phApp.ddo.jobDetail) for the full text.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text, post_json
from appa.sources.text import html_to_text, mark_hidden

PAGE = 50  # rows per search request
MAX_PAGES = 50  # per query
PAUSE = 1.0  # seconds between requests
MAX_DESC = 100_000
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
US = "United States of America"  # Phenom's country facet value

_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CC = re.compile(r"^(?:[a-z]{2}|global)$")
_LANG = re.compile(r"^[a-z]{2}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF = re.compile(r"^[A-Z0-9]{2,30}$")
_LOCALE = re.compile(r"^[a-z]{2}_[a-z]{2,8}$")
_SITE_TYPE = re.compile(r"^[a-z]{1,20}$")
_META = re.compile(r"<meta\b[^>]*>", re.I)
_TYPES = {
    "full time": "full_time",
    "full-time": "full_time",
    "part time": "part_time",
    "part-time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "seasonal": "temporary",
}
_NOT_NAMES = {"www", "careers", "career", "jobs", "job", "apply", "join", "work", "talent", "recruiting"}


def parse_slug(slug: str) -> tuple[str, str]:
    """(domain, locale path like "/us/en" or "") from a "domain[:cc:lang]" slug; ValueError if malformed."""
    parts = (slug or "").split(":")
    domain = parts[0].lower()
    if not _DOMAIN.match(domain) or len(parts) not in (1, 3):
        raise ValueError(f"bad Phenom slug {slug!r}: expected domain or domain:cc:lang")
    if len(parts) == 1:
        return domain, ""
    cc, lang = parts[1].lower(), parts[2].lower()
    if not (_CC.match(cc) and _LANG.match(lang)):
        raise ValueError(f"bad Phenom slug {slug!r}")
    return domain, f"/{quote(cc, safe='')}/{quote(lang, safe='')}"


def _slug(domain: str, prefix: str) -> str:
    """The slug for a domain and locale path."""
    return ":".join([domain, *[p for p in prefix.split("/") if p]])


def _base(domain: str, prefix: str) -> str:
    """The site's origin plus locale path, built only from validated parts."""
    return f"https://{domain}{prefix}"


def _job_url(domain: str, prefix: str, job_id: str) -> str:
    """The human posting page for one job."""
    return f"{_base(domain, prefix)}/job/{quote(job_id, safe='')}"


def _parts(url: str) -> tuple[str, str, list[str]] | None:
    """(domain, locale path, remaining path segments) of a Phenom-style career link, or None."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if not _DOMAIN.match(host):
        return None
    segs = [s for s in p.path.split("/") if s]
    if len(segs) >= 3 and _CC.match(segs[0].lower()) and _LANG.match(segs[1].lower()):
        return host, f"/{segs[0].lower()}/{segs[1].lower()}", segs[2:]
    return host, "", segs


def _is_job(rest: list[str]) -> bool:
    """True for a job/{id}[/{title}] path whose id looks like a requisition (has a digit)."""
    return len(rest) in (2, 3) and rest[0] == "job" and bool(_ID.match(rest[1])) and any(c.isdigit() for c in rest[1])


def board_of(url: str) -> str | None:
    """The slug of a Phenom career-site link (/{cc}/{lang}/search-results, /{cc}/{lang}/job/{id}/..., /search-results,
    or any job link on a phenompeople.com host), or None for any other link."""
    got = _parts(url)
    if not got:
        return None
    domain, prefix, rest = got
    phenom_host = domain == "phenompeople.com" or domain.endswith(".phenompeople.com")
    if rest == ["search-results"] or ((prefix or phenom_host) and _is_job(rest)):
        return _slug(domain, prefix)
    return None


def _object_after(page: str, marker: str) -> dict:
    """The JSON object a page's script assigns after `marker` (e.g. "phApp.ddo = "), or {}."""
    i = page.find(marker)
    if i < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(page, i + len(marker))
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _meta(page: str, prop: str) -> str:
    """The content of a <meta property=prop> tag, unescaped, or ""."""
    for tag in _META.findall(page):
        if re.search(rf"""\bproperty\s*=\s*["']{re.escape(prop)}["']""", tag, re.I):
            m = re.search(r"""\bcontent\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.I)
            if m:
                return " ".join(html.unescape(m.group(1) or m.group(2) or "").split())
    return ""


def _company_name(page: str, domain: str) -> str:
    """The company name from og:site_name, else "... at X" in og:title, else from the domain."""
    name = _meta(page, "og:site_name")
    if not name:
        m = re.search(r"\bat\s+(.+)$", _meta(page, "og:title"))
        name = m.group(1).strip() if m else ""
    if name:
        return name[:200]
    labels = [x for x in domain.split(".")[:-1] if x not in _NOT_NAMES]
    return (labels[-1] if labels else domain).replace("-", " ").title()


def _site(domain: str, prefix: str) -> dict:
    """The tenant's search settings (refNum, locale, country, siteType, company) from its search page."""
    page, final = get_text(f"{_base(domain, prefix)}/search-results")
    if (urlparse(final).hostname or "").lower().rstrip(".") != domain:
        raise ValueError(f"{domain} redirected to another host; not a Phenom career site")
    cfg = _object_after(page, "phApp = phApp || ")
    ref = str(cfg.get("refNum") or "")
    if not _REF.match(ref):
        m = re.search(r'"refNum":"([A-Z0-9]{2,30})"', page)
        ref = m.group(1) if m else ""
    if not ref:
        raise ValueError(f"{domain} is not a Phenom career site (no refNum)")
    locale, country, site_type = (str(cfg.get(k) or "").lower() for k in ("locale", "country", "siteType"))
    return {
        "refNum": ref,
        "lang": locale if _LOCALE.match(locale) else "en_us",
        "country": country if _CC.match(country) else "us",
        "siteType": site_type if _SITE_TYPE.match(site_type) else "external",
        "company": _company_name(page, domain),
    }


def _day(value) -> date | None:
    """A date from Phenom's "2026-10-27T00:00:00.000+0000", or None."""
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _strs(value) -> list[str]:
    """Non-empty strings from a list field (anything else gives [])."""
    return [" ".join(str(x).split()) for x in value if x not in (None, "")] if isinstance(value, list) else []


def _apply_link(row: dict) -> str:
    """The underlying ATS posting link (applyUrl without /apply), when it is a clean https link off this site."""
    url = str(row.get("applyUrl") or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if p.scheme != "https" or not p.hostname or any(c.isspace() for c in url) or len(url) > 500:
        return ""
    return url[: -len("/apply")] if url.endswith("/apply") else url


def _listing(row: dict, domain: str, prefix: str, company: str) -> Listing | None:
    """A Listing from one search row, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    job_id, title = str(row.get("jobId") or "").strip(), str(row.get("title") or "")
    if not (_ID.match(job_id) and title.strip()):
        return None
    locs = _strs(row.get("multi_location"))
    kind = str(row.get("type") or "").strip()
    cats = _strs(row.get("multi_category")) or _strs([row.get("category")])
    hints = {
        "category": ", ".join(dict.fromkeys(cats)),
        "country": str(row.get("country") or ""),
        "employment_type": _TYPES.get(kind.lower(), kind.lower().replace(" ", "_")),
        "links": _apply_link(row),
    }
    try:
        return Listing(
            company=company,
            title=title,
            url=_job_url(domain, prefix, job_id),
            location=("; ".join(locs) or str(row.get("cityStateCountry") or row.get("location") or ""))[:500],
            posted_at=_day(row.get("postedDate")) or _day(row.get("dateCreated")),
            ats="phenom",
            ats_job_id=job_id,
            source_ref=str(row.get("jobSeqNo") or row.get("reqId") or job_id)[:300],
            hints=hints,
        )
    except ValueError:
        return None


def _search(domain: str, site: dict, query: str, start: int, us: bool) -> dict:
    """One refineSearch page: {"totalHits", "data": {"jobs": [...]}}, or {} if the reply is malformed."""
    body = {
        "lang": site["lang"],
        "deviceType": "desktop",
        "country": site["country"],
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "Most recent",
        "from": start,
        "size": PAGE,
        "jobs": True,
        "counts": True,
        "keywords": query,
        "global": True,
        "selected_fields": {"country": [US]} if us else {},
        "refNum": site["refNum"],
        "siteType": site["siteType"],
    }
    data = post_json(f"https://{domain}/widgets", body, headers=HEADERS)
    got = data.get("refineSearch") if isinstance(data, dict) else None
    return got if isinstance(got, dict) else {}


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Open postings on one Phenom career site: one search per opts.queries, capped by opts.max_results."""
    opts = opts or FeedOptions()
    domain, prefix = parse_slug(slug)
    site = _site(domain, prefix)
    out: list[Listing] = []
    seen: set[str] = set()
    for query in opts.queries or ("",):
        start, us, last = 0, opts.us_only, None
        for _ in range(MAX_PAGES):
            if len(out) >= opts.max_results:
                return out[: opts.max_results]
            time.sleep(PAUSE)
            res = _search(domain, site, query, start, us)
            total = res.get("totalHits")
            if us and start == 0 and total == 0:  # this tenant spells the country another way
                us = False
                time.sleep(PAUSE)
                res = _search(domain, site, query, start, us)
                total = res.get("totalHits")
            data = res.get("data")
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not rows or not isinstance(rows, list):
                break
            ids = [r.get("jobId") for r in rows if isinstance(r, dict)]
            if ids == last:  # the API repeated the previous page: this query is done
                break
            last = ids
            for row in rows:
                x = _listing(row, domain, prefix, site["company"])
                if x is None or (opts.since and x.posted_at and x.posted_at < opts.since):
                    continue
                if x.ats_job_id not in seen:  # seen spans queries only to dedupe the output
                    seen.add(x.ats_job_id)
                    out.append(x)
            start += len(rows)
            if not isinstance(total, int) or start >= total:
                break
    return out[: opts.max_results]


def describe(listing: Listing) -> str:
    """The posting's full text, from the job record embedded in its Phenom posting page (one request)."""
    got = _parts(listing.url)
    if not got or not _is_job(got[2][:2]):
        raise ValueError("not a Phenom posting link")
    domain, prefix, rest = got
    page, _ = get_text(_job_url(domain, prefix, rest[1]))
    detail = _object_after(page, "phApp.ddo = ").get("jobDetail")
    data = detail.get("data") if isinstance(detail, dict) else None
    job = data.get("job") if isinstance(data, dict) else None
    raw = str(job.get("description") or "") if isinstance(job, dict) else ""
    if not raw:
        return ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="phenom",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="careers.mastercard.com:us:en",
        terms="Public career-site search (no login or key), the same POST /widgets call the site's search page makes; "
        "robots.txt disallows only */apply, */jobcart, */chatbot and tracking scripts. appa paces 1s per request "
        "and reads at most 50 pages of 50 per query.",
        hosts=(),
    )
)
