"""JOIN (join.com) company job boards, through the same public endpoints its careers pages use (no key).

JOIN is an ATS for mostly European small and mid-sized employers. A board lives at
https://join.com/companies/{slug}. Two steps per board:

1. GET the board page (Next.js) and read its <script id="__NEXT_DATA__"> JSON for the numeric company id
   and company name (under props.pageProps.pageData.company, or initialState.company on older pages).
2. GET https://join.com/api/public/companies/{companyId}/jobs?page=N&pageSize=5 (pageSize must be 1-5)
   until page == pagination.pageCount. Items carry id, idParam, title, createdAt, workplaceType, city,
   country, employmentType, category and salary (in cents) but no description.

The number in idParam changes whenever the ad is republished (old links redirect), so Listing.url is built
from the stable `id` instead: https://join.com/companies/{slug}/{id} redirects to the current posting and
keeps appa's dedupe key (join.com:{id}) the same across reposts. describe() reads the posting page's
__NEXT_DATA__ for the description HTML. Each repost adds a redirect hop; if a chain ever passes the 5-hop
limit of appa.sources.http, describe() retries once with the bare-id form, and failing that the error is
raised (appa records it). robots.txt allows everything except /*/lp/.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import BlockedURL, get_json, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

BASE = "https://join.com"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ID_PARAM = re.compile(r"^[0-9]{1,15}(-[A-Za-z0-9-]{0,300})?$")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_HOSTS = ("join.com", "www.join.com")
_REMOTE = {"REMOTE": "yes", "HYBRID": "hybrid", "ONSITE": "no"}
PAGE_SIZE = 5  # the API answers 422 above 5
MAX_PAGES = 50
PAUSE = 0.5
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, lowercased, if it is a safe JOIN company slug; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith((".xml", ".txt")):
        raise ValueError(f"bad join.com slug: {slug!r}")
    return slug.lower()


def board_url(slug: str) -> str:
    """The company's board page on join.com."""
    return f"{BASE}/companies/{quote(_check(slug), safe='')}"


def _next_data(page: str) -> dict:
    """The Next.js __NEXT_DATA__ pageProps of a join.com page ({} when absent or unreadable)."""
    m = _NEXT_DATA.search(page or "")
    try:
        props = json.loads(m.group(1))["props"]["pageProps"] if m else {}
    except (ValueError, KeyError, TypeError):
        return {}
    return props if isinstance(props, dict) else {}


def _state(props: dict) -> dict:
    """The page state dict: pageData on current pages, initialState on older ones."""
    for k in ("pageData", "initialState"):
        if isinstance(props.get(k), dict):
            return props[k]
    return {}


def _company(slug: str) -> tuple[int, str]:
    """(companyId, company name) from the board page; ValueError when the page has no company."""
    page, _ = get_text(board_url(slug))
    co = _state(_next_data(page)).get("company")
    cid = str(co.get("id") or "") if isinstance(co, dict) else ""
    if not cid.isdigit():
        raise ValueError(f"no join.com company on board {slug!r}")
    return int(cid), str(co.get("name") or slug)


def _day(v) -> date | None:
    """'2026-10-05T13:33:03.404Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _name(v) -> str:
    """The 'name' of a {id, name} object, or ''."""
    return str(v.get("name") or "") if isinstance(v, dict) else ""


def _salary(j: dict) -> str:
    """'90000-105000 EUR/year' from the cents amounts, or '' when absent or hidden."""
    if isinstance(j.get("settings"), dict) and j["settings"].get("showSalary") is False:
        return ""
    lo, hi = j.get("salaryAmountFrom"), j.get("salaryAmountTo")
    amounts = [
        a for a in (lo, hi) if isinstance(a, dict) and isinstance(a.get("amount"), (int, float)) and a["amount"] > 0
    ]
    if not amounts:
        return ""
    rng = "-".join(f"{a['amount'] / 100:g}" for a in amounts)
    cur = str(amounts[0].get("currency") or "")
    freq = str(j.get("salaryFrequency") or "").removeprefix("PER_").lower()
    return " ".join(x for x in (rng, cur + (f"/{freq}" if freq else "")) if x)


def _employment(name: str) -> str:
    """JOIN's employment type name mapped to HINT_KEYS words where it is clear, else ''."""
    n = name.lower()
    if any(w in n for w in ("intern", "trainee", "apprentice", "praktik")):
        return "internship"
    if "freelanc" in n or "contract" in n:
        return "contract"
    if "part" in n or "mini" in n or "student" in n:
        return "part_time"
    if n in ("employee", "full-time", "full time", "fulltime", "festanstellung", "vollzeit"):
        return "full_time"
    return ""


def _location(j: dict) -> tuple[str, str]:
    """(location text, country code/name) from the item's city and country."""
    city = j.get("city") if isinstance(j.get("city"), dict) else {}
    country = j.get("country") if isinstance(j.get("country"), dict) else {}
    cname = str(city.get("countryName") or country.get("name") or "")
    text = ", ".join(x for x in (str(city.get("cityName") or ""), cname) if x)
    if j.get("workplaceType") == "REMOTE":
        text = f"Remote ({text})" if text else "Remote"
    return text, ", ".join(x for x in (str(country.get("iso3166") or ""), cname) if x)


def _listing(j: dict, slug: str, company: str) -> Listing:
    """One API item as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict):
        raise ValueError("not a job")
    jid = str(j.get("id") or "")
    if not jid.isdigit() or len(jid) > 15:
        raise ValueError("job without a usable id")
    location, country = _location(j)
    hints = {
        "remote": _REMOTE.get(str(j.get("workplaceType") or ""), ""),
        "country": country,
        "salary": _salary(j),
        "employment_type": _employment(_name(j.get("employmentType"))),
        "category": _name(j.get("category")),
    }
    return Listing(
        company=company,
        title=str(j.get("title") or ""),
        url=f"{BASE}/companies/{quote(slug, safe='')}/{jid}",
        location=location,
        posted_at=_day(j.get("createdAt")),
        ats="join_com",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's JOIN board (no descriptions; see describe)."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    cid, company = _company(slug)
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        data = get_json(f"{BASE}/api/public/companies/{cid}/jobs", params={"page": page, "pageSize": PAGE_SIZE})
        items = (data.get("items") if isinstance(data, dict) else None) or []
        for j in items:
            try:
                x = _listing(j, slug, company)
            except ValueError as e:
                log.debug("skipping join.com job on %s: %s", slug, e)
                continue
            if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        pagination = data.get("pagination") if isinstance(data, dict) else None
        count = pagination.get("pageCount") if isinstance(pagination, dict) else None
        if not items or not isinstance(count, int) or page >= count:
            break
    return out


def describe(listing: Listing) -> str:
    """The posting's description text, from its join.com page (one request)."""
    p = urlparse(listing.url)
    parts = p.path.strip("/").split("/")
    if (
        p.scheme != "https"
        or (p.hostname or "").lower() not in _HOSTS
        or len(parts) != 3
        or parts[0] != "companies"
        or not _ID_PARAM.fullmatch(parts[2])
    ):
        raise ValueError("not a join.com posting link")
    base = f"{BASE}/companies/{quote(_check(parts[1]), safe='')}/"
    try:
        page, _ = get_text(base + quote(parts[2], safe=""))
    except BlockedURL as e:
        bare = parts[2].split("-", 1)[0]
        if "redirects" not in str(e) or bare == parts[2]:
            raise
        page, _ = get_text(base + bare)  # one fewer hop than the titled link
    job = _state(_next_data(page)).get("job")
    raw = str(job.get("description") or "") if isinstance(job, dict) else ""
    text, hidden = html_to_text(raw)
    return mark_hidden(text, hidden)[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The company slug of a join.com/companies/{slug}[/{id}-{title}] link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host not in _HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) not in (2, 3) or parts[0] != "companies" or (len(parts) == 3 and not _ID_PARAM.fullmatch(parts[2])):
        return None
    try:
        return _check(parts[1])
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="join_com",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="bexio",
        terms="Public careers pages and /api/public jobs endpoint (no key); robots.txt allows all but /*/lp/. "
        "JOIN's company terms bar automated load, so keep it light: 1 page request + 1 per 5 jobs, paced.",
        hosts=("join.com",),
    )
)
