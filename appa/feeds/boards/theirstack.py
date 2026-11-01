"""TheirStack job search API: postings aggregated from company career sites, ATSs and job boards.

POST https://api.theirstack.com/v1/jobs/search with "Authorization: Bearer <THEIRSTACK_API_KEY>" (free signup at
theirstack.com) and a JSON body of filters returns {metadata, data}. Each job has id, job_title, url (the
posting where TheirStack found it), final_url (the employer's own careers/ATS page, when known), date_posted,
company_object {name, domain}, locations, remote/hybrid, seniority, employment_statuses, salary fields and a
markdown description. Paging is offset/limit in the body. Every request needs a date or company filter.

Cost and limits: 1 API credit per job returned (the free plan's monthly allowance is small; see the dashboard)
and the free tier allows 4/second, 10/minute, 50/hour, 400/day. So every fetch is capped at CREDIT_BUDGET jobs,
sends tight filters and pauses between pages. Two entry points share it:

- the list feed "theirstack": one search over all FeedOptions queries (titles, OR-ed), junior seniority,
  US only by default, posted since opts.since (else the last day). Run it about once a day.
- the board "theirstack": one company's recent postings by its website domain (slug "stripe.com"). Postings
  never link to TheirStack, so boards are only found from TheirStack links that carry a company domain.

Terms (theirstack.com/en/docs/legal/terms-and-conditions): internal use and even public display of job postings
is allowed; reselling the data as a competing product and scraping the website are not. API keys may not be
shared. robots.txt only disallows /api/search on the website; this is the documented API.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

from appa.core.ats_urls import parse_url
from appa.feeds import BoardFeed, FeedOptions, ListFeed, Listing, register_board, register_list
from appa.sources.http import post_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

URL = "https://api.theirstack.com/v1/jobs/search"
KEY_ENV = "THEIRSTACK_API_KEY"
CREDIT_BUDGET = 50  # most jobs (= credits) one fetch may return
PAGE = 25  # jobs per request
MAX_PAGES = 50
PAUSE = 6.5  # seconds between pages: the free tier allows 10 requests a minute
BOARD_DAYS = 30  # a board fetch without opts.since looks back this many days
MAX_DESC = 100_000
_DOMAIN = re.compile(r"^(?=.{4,100}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LEVELS = {"junior": "entry", "mid_level": "mid", "senior": "senior", "staff": "senior", "c_level": "senior"}


def _check(slug: str) -> str:
    """The slug, if it is a plain lowercase company domain like 'stripe.com'; else ValueError."""
    if not isinstance(slug, str) or not _DOMAIN.fullmatch(slug.lower()):
        raise ValueError(f"bad theirstack slug: {slug!r}")
    return slug.lower()


def _host(url) -> str:
    """Lowercase hostname of a link without 'www.', or ''."""
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _key() -> str:
    """The API key from the environment; RuntimeError when it is unset."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    return key


def _day(v) -> date | None:
    """'2026-10-30' (or an ISO datetime) -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _http(v) -> str:
    """The value if it is an http(s) link, else ''."""
    v = str(v or "").strip()
    return v if v.lower().startswith(("http://", "https://")) else ""


def _salary(r: dict) -> str:
    """The posted pay as the source shows it, else the annual range, else ''."""
    if r.get("salary_string"):
        return str(r["salary_string"])
    lo, hi = (str(r.get(k) or "").split(".")[0] for k in ("min_annual_salary", "max_annual_salary"))
    rng = f"{lo}-{hi}" if lo and hi else f"{lo}+" if lo else f"up to {hi}" if hi else ""
    return f"{rng} {r.get('salary_currency') or ''}".strip() + "/year" if rng else ""


def _location(r: dict) -> str:
    """All the job's locations, else the short location."""
    names = [str(x.get("display_name") or "") for x in r.get("locations") or [] if isinstance(x, dict)]
    names = [n for n in dict.fromkeys(names) if n] or [str(r.get("short_location") or r.get("location") or "")]
    return "; ".join(names)[:500]


def _hints(r: dict, other_link: str) -> dict[str, str]:
    """The facts the posting states, under HINT_KEYS names."""
    types = [str(t) for t in r.get("employment_statuses") or [] if t]
    level = "intern" if "internship" in types else _LEVELS.get(str(r.get("seniority")), "")
    remote = (
        "yes"
        if r.get("remote")
        else "hybrid"
        if r.get("hybrid")
        else "no"
        if r.get("remote") is False and r.get("hybrid") is False
        else ""
    )
    codes = [str(c) for c in r.get("country_codes") or [] if c] or (
        [str(r["country_code"])] if r.get("country_code") else []
    )
    return {
        "level": level,
        "remote": remote,
        "country": ", ".join(dict.fromkeys(codes)),
        "salary": _salary(r),
        "employment_type": ", ".join(types),
        "links": other_link,
    }


def _description(r: dict) -> str:
    """The description (markdown that may hold HTML) as text, hidden text flagged."""
    text, hidden = html_to_text(str(r.get("description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(r: dict) -> Listing:
    """One job as a Listing (ValueError when it is malformed, closed or has no link)."""
    if not isinstance(r, dict):
        raise ValueError("not a job")
    if r.get("closed_at"):
        raise ValueError("closed posting")
    if r.get("has_blurred_data"):
        raise ValueError("blurred posting")
    co = r.get("company_object") if isinstance(r.get("company_object"), dict) else {}
    source, final = _http(r.get("url")), _http(r.get("final_url"))
    url = final or source
    ref = parse_url(url)
    tid = str(r.get("id") or "").strip() or None
    jid = ref.job_id if ref and ref.job_id else tid
    return Listing(
        company=str(co.get("name") or r.get("company") or ""),
        title=str(r.get("job_title") or ""),
        url=url,
        location=_location(r),
        posted_at=_day(r.get("date_posted")),
        description=_description(r),
        ats=ref.ats if ref else "unknown",
        ats_job_id=jid,
        source_ref=tid,
        hints=_hints(r, source if source != url else ""),
    )


def _search(body: dict, opts: FeedOptions, keep=None) -> list[Listing]:
    """Run one filtered search (newest first, the API default), offset-paged up to the credit budget."""
    headers = {"Authorization": f"Bearer {_key()}"}
    budget = max(0, min(opts.max_results, CREDIT_BUDGET))
    out: dict[str, Listing] = {}
    seen = 0
    for page in range(MAX_PAGES):
        limit = min(PAGE, budget - seen)
        if limit <= 0:
            break
        if page:
            time.sleep(PAUSE)
        data = post_json(URL, {**body, "offset": seen, "limit": limit}, headers=headers)
        rows = data.get("data") if isinstance(data, dict) else None
        rows = rows if isinstance(rows, list) else []
        seen += len(rows)
        for r in rows:
            try:
                x = _listing(r)
            except ValueError as e:
                log.debug("skipping theirstack job: %s", e)
                continue
            if keep is None or keep(r):
                out.setdefault(x.ats_job_id or x.url, x)
        if len(rows) < limit:
            break
    return list(out.values())[: opts.max_results]


def _dates(opts: FeedOptions, days: int) -> dict:
    """The date filter: posted since opts.since, else within the last `days` days."""
    return {"posted_at_gte": opts.since.isoformat()} if opts.since else {"posted_at_max_age_days": days}


def fetch_list(opts: FeedOptions) -> list[Listing]:
    """Newest junior postings whose titles match any query (US only by default), up to the credit budget."""
    _key()
    titles = [q for q in dict.fromkeys(" ".join(str(q).split()) for q in opts.queries) if 3 <= len(q) <= 100][:20]
    if not titles:
        return []
    body = {
        "job_title_or": titles,
        "job_seniority_or": ["junior"],
        **_dates(opts, 1),
        **({"job_country_code_or": ["US"]} if opts.us_only else {}),
    }
    return _search(body, opts)


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Recent postings of the company whose website is this domain, up to the credit budget."""
    slug, opts = _check(slug), opts or FeedOptions()
    _key()
    body = {
        "company_domain_or": [slug],
        **_dates(opts, BOARD_DAYS),
        **({"job_country_code_or": ["US"]} if opts.us_only else {}),
    }

    def same_company(r: dict) -> bool:
        """Keep jobs whose company domain is this one (or unknown, since the filter already matched)."""
        co = r.get("company_object") if isinstance(r.get("company_object"), dict) else {}
        d = str(co.get("domain") or r.get("company_domain") or "").lower()
        return not d or d == slug

    return _search(body, opts, keep=same_company)


def board_of(url: str) -> str | None:
    """The company domain in a theirstack.com link that names one (?company_domain=... or company_domain_or=...)."""
    host = _host(url)
    if host != "theirstack.com" and not host.endswith(".theirstack.com"):
        return None
    try:
        qs = parse_qs(urlparse(url.strip()).query)
    except ValueError:
        return None
    vals = (qs.get("company_domain") or []) + (qs.get("company_domain_or") or [])
    if len(vals) != 1:
        return None
    v = vals[0].strip()
    try:
        return _check(_host(v) if "://" in v else v.removeprefix("www."))
    except ValueError:
        return None


_TERMS = (
    f"Needs {KEY_ENV} (free signup at theirstack.com). 1 API credit per job returned; the free plan has a "
    f"small monthly allowance, so each fetch stops at {CREDIT_BUDGET} jobs: run about once a day. Free tier "
    "rate limits 4/s, 10/min, 50/h, 400/day. Internal use allowed; no reselling, no sharing the key."
)

FEED = register_list(
    ListFeed(
        name="theirstack",
        fetch=fetch_list,
        key_env=KEY_ENV,
        terms=_TERMS,
        description="TheirStack API: junior postings aggregated from career sites, ATSs and boards (US, newest first).",
    )
)

BOARD = register_board(
    BoardFeed(
        ats="theirstack",
        fetch=fetch,
        board_of=board_of,
        example="stripe.com",
        terms=_TERMS,
        hosts=("api.theirstack.com",),
    )
)
