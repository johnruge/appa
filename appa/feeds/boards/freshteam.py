"""Freshteam (Freshworks) job boards, read from each company's public careers page (no key).

GET https://{slug}.freshteam.com/jobs is the server-rendered careers page with every open job (no paging).
It's the route robots.txt allows (`Allow: /jobs`, `Disallow: /`): the careers-widget JSON under /hire/ is
off limits to crawlers, so it isn't used. Each job is a `.heading` card linking /jobs/{unique_id}/{title-slug}
with a title, location text and (on the default template) data-portal-location ("City, Country"),
data-portal-job-type (2 = full time, 4 = part time) and data-portal-remote-location; the card's department
is the enclosing role-title <h5>. The page states no posting date, so posted_at stays None. The company
name comes from og:title ("Careers - Acme"). describe() reads one posting page, also under /jobs, and takes
the text from its schema.org JobPosting JSON-LD. Dead tenants answer a 200 "invalid" page (no cards) or
a 404. Freshworks is winding Freshteam down, so boards may disappear.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under freshteam.com
_JOB_PATH = re.compile(r"^/jobs/([A-Za-z0-9_-]{4,40})(?:/([A-Za-z0-9._~%-]{0,200}))?/?$")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "assets",
    "support",
    "help",
    "blog",
    "status",
    "cdn",
    "static",
    "mail",
    "login",
    "signup",
    "developers",
    "partners",
    "community",
    "jobs",
    "careers",
}
_TYPE_CODES = {"2": "full_time", "4": "part_time"}  # data-portal-job-type, as the page's own filter labels them
_TYPE_WORDS = {
    "full time": "full_time",
    "part time": "part_time",
    "contract": "contract",
    "internship": "internship",
    "temporary": "temporary",
    "freelance": "freelance",
}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Freshteam subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad freshteam slug: {slug!r}")
    return slug.lower()


def board_url(slug: str) -> str:
    """The careers page URL for a board."""
    return f"https://{quote(_check(slug), safe='')}.freshteam.com/jobs"


def _company(soup: BeautifulSoup, slug: str) -> str:
    """The company name from og:title ('Careers - Acme') or <title> ('Acme - Careers'), else the slug."""
    og = soup.find("meta", attrs={"property": "og:title"})
    name = str(og.get("content") or "").strip() if og else ""
    if name.lower().startswith("careers - "):
        return name[len("careers - ") :].strip()[:300] or slug
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title.lower().endswith(" - careers"):
        return title[: -len(" - careers")].strip()[:300] or slug
    return slug


def _category(card) -> str:
    """The department heading (role-title <h5>) of the group a card sits in."""
    group = card.find_parent(lambda t: t.name == "li" and t.find("div", class_="role-title", recursive=False))
    h5 = group.find("div", class_="role-title", recursive=False).find("h5") if group else None
    first = h5.find(string=True, recursive=False) if h5 else None
    return " ".join(str(first or "").split())[:300]


def _listing(card, slug: str, company: str) -> Listing:
    """One job card as a Listing (ValueError when its link, title or ids are missing or bad)."""
    link = card if card.name == "a" and card.get("href") else card.find("a", href=True)
    p = urlparse(str(link.get("href") if link else ""))
    if p.netloc and (p.hostname or "").lower() != f"{slug}.freshteam.com":
        raise ValueError("link off the board")
    m = _JOB_PATH.fullmatch(p.path)
    if not m:
        raise ValueError("not a job link")
    uid, tail = m.group(1), m.group(2) or ""
    url = f"https://{slug}.freshteam.com/jobs/{quote(uid, safe='')}" + (f"/{quote(tail, safe='%')}" if tail else "")
    title_el = card.find(class_="job-title")
    info = card.find(class_="location-info")
    lines = list(info.stripped_strings) if info else []
    where = str(card.get("data-portal-location") or "").strip()
    remote = str(card.get("data-portal-remote-location") or "").strip().lower()
    place = where or (lines[0] if lines and lines[0].lower() != "remote" else "")
    location = (f"Remote ({place})" if place else "Remote") if remote == "true" else place
    kind = _TYPE_CODES.get(str(card.get("data-portal-job-type") or ""), "") or next(
        (_TYPE_WORDS[x.lower()] for x in lines[1:] if x.lower() in _TYPE_WORDS), ""
    )
    hints = {
        "remote": {"true": "yes", "false": "no"}.get(remote, ""),
        "employment_type": kind,
        "country": where.rsplit(",", 1)[1].strip() if "," in where else "",
        "category": _category(card),
    }
    return Listing(
        company=company,
        title=title_el.get_text(" ", strip=True) if title_el else "",
        url=url,
        location=location[:500],
        ats="freshteam",
        ats_job_id=uid,
        source_ref=uid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this company's Freshteam careers page (one request; descriptions via describe())."""
    opts = opts or FeedOptions()
    slug = _check(slug)
    try:
        page, _ = get_text(board_url(slug))
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 410):  # tenant gone
            return []
        raise
    soup = BeautifulSoup(page, "html.parser")
    company = _company(soup, slug)
    out, seen = [], set()
    for card in soup.select(".heading"):
        try:
            x = _listing(card, slug, company)
        except ValueError as e:
            log.debug("skipping freshteam card on %s: %s", slug, e)
            continue
        if x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def _posting(soup: BeautifulSoup) -> dict:
    """The page's schema.org JobPosting JSON-LD as a dict, or {}."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "", strict=False)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d
    return {}


def describe(listing: Listing) -> str:
    """The posting's text, from the JobPosting JSON-LD on its Freshteam page (one request); '' if none."""
    p = urlparse(listing.url)
    host = (p.hostname or "").lower()
    m = _JOB_PATH.fullmatch(p.path)
    if not host.endswith(".freshteam.com") or not m:
        raise ValueError("not a freshteam posting link")
    slug = _check(host[: -len(".freshteam.com")])
    page, _ = get_text(f"https://{slug}.freshteam.com/jobs/{quote(m.group(1), safe='')}")
    text, hidden = html_to_text(str(_posting(BeautifulSoup(page, "html.parser")).get("description") or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The board slug of a {slug}.freshteam.com/jobs link, or None (custom career domains aren't recognised)."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if not host.endswith(".freshteam.com"):
        return None
    label = host[: -len(".freshteam.com")]
    path = p.path.rstrip("/")
    if "." in label or label in _NOT_BOARDS or not (path == "/jobs" or path.startswith("/jobs/")):
        return None
    try:
        return _check(label)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="freshteam",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="talent360",
        terms="Public careers page {slug}.freshteam.com/jobs, the path robots.txt allows (Allow: /jobs, Disallow: /; "
        "the /hire/ widget JSON is not used). One request per board, one per posting for describe().",
        hosts=("freshteam.com",),
    )
)
