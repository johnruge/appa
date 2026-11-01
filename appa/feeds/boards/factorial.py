"""Factorial career sites, from each company's public server-rendered job list (no key).

GET https://{slug}.factorial.com/ (formerly {slug}.factorialhr.com, which now 301-redirects there) lists
every open posting on one page: each <li class="job-offer-item"> carries the canonical posting link
(data-job-postings-url = https://{slug}.factorial.com/job_posting/{title-slug}-{id}), the contract type,
a remote flag and a location key, and shows the title, team and workplace (Onsite/Remote/Hybrid). Postings
are grouped under <h3> city headings; a posting listed under several cities is merged into one Listing.
The numeric id at the end of the posting link is the stable job ID. The page title is
"{Company} - Job offers, offices and team". The list has no description or date, so describe() fetches the
posting page (its rich-text body, falling back to the JSON-LD JobPosting). robots.txt: "Allow: /".
Factorial is common in Spain and Portugal.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under factorial.com
_JOB_ID = re.compile(r"^/job_posting/(?:[^/]*-)?(\d{1,15})/?$")
_DOMAINS = ("factorial.com", "factorialhr.com")
_NOT_BOARDS = {
    "www",
    "app",
    "api",
    "apps",
    "help",
    "support",
    "blog",
    "status",
    "docs",
    "developers",
    "academy",
    "community",
    "partners",
    "marketplace",
    "cdn",
    "static",
    "assets",
    "media",
    "images",
    "mail",
    "auth",
    "login",
    "signup",
    "id",
    "go",
    "info",
    "resources",
    "es",
    "en",
    "fr",
    "de",
    "it",
    "pt",
    "br",
    "mx",
}
_TYPES = {
    "indefinite": "full_time",
    "permanent": "full_time",
    "full_time": "full_time",
    "part_time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "temporary": "temporary",
    "freelance": "contract",
    "vendor_contractor": "contract",
    "contractor": "contract",
}
_WORKPLACE = {"remote": "yes", "hybrid": "hybrid", "onsite": "no", "on-site": "no", "on site": "no"}
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Factorial subdomain label; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith("-"):
        raise ValueError(f"bad factorial slug: {slug!r}")
    return slug.lower()


def board_url(slug: str) -> str:
    """The career site's job list URL."""
    return f"https://{quote(_check(slug), safe='')}.factorial.com/"


def job_id(url: str) -> str | None:
    """The numeric posting ID at the end of a /job_posting/{title}-{id} link, or None."""
    try:
        m = _JOB_ID.match(urlparse(url).path)
    except (AttributeError, ValueError):
        return None
    return m.group(1) if m else None


def _company(soup: BeautifulSoup, slug: str) -> str:
    """The company name from the page title ('{Company} - Job offers, offices and team'), else the slug."""
    title = " ".join((soup.title.get_text() if soup.title else "").split())
    name = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
    return name[:300] or slug


def _texts(li) -> list[str]:
    """The item's visible text cells in order: title, then team (only when it has one), then workplace."""
    return [" ".join(d.get_text(" ", strip=True).split()) for d in li.select("div.text-left")]


def _city(li) -> str:
    """The <h3> city heading of the group this item is listed under."""
    h3 = li.find_previous("h3")
    return " ".join(h3.get_text(" ", strip=True).split()) if h3 else ""


def _listing(li, company: str) -> Listing:
    """One job-offer-item as a Listing (ValueError when the link, ID or title is missing or bad)."""
    url = (li.get("data-job-postings-url") or "").strip()
    a = li.find("a", href=True)
    url = url or (a["href"].strip() if a else "")
    jid = job_id(url)
    if not jid:
        raise ValueError(f"no job_posting id in {url!r}")
    cells = _texts(li) or [""]
    title, rest = cells[0], cells[1:]
    workplace = rest[-1] if rest and rest[-1].lower() in _WORKPLACE else ""
    team = rest[0] if li.get("data-team-id") and rest and (len(rest) > 1 or not workplace) else ""
    remote = "yes" if li.get("data-is-remote") == "true" else _WORKPLACE.get(workplace.lower(), "")
    city = _city(li)
    location = "Remote" if remote == "yes" and city.lower() in ("", "remote") else city
    if remote == "yes" and location != "Remote":
        location = f"Remote ({location})"
    ctype = (li.get("data-contract-type") or "").strip().lower()
    hints = {"remote": remote, "category": team, "employment_type": _TYPES.get(ctype, ctype)}
    return Listing(
        company=company,
        title=title,
        url=url,
        location=location,
        ats="factorial",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this Factorial career site, without descriptions (one page, no paging)."""
    opts = opts or FeedOptions()
    html, _ = get_text(board_url(slug))
    soup = BeautifulSoup(html, "html.parser")
    company = _company(soup, slug)
    out: dict[str, Listing] = {}
    for li in soup.select("li.job-offer-item"):
        try:
            x = _listing(li, company)
        except ValueError as e:
            log.debug("skipping factorial item on %s: %s", slug, e)
            continue
        if x.ats_job_id in out:  # same posting under another city: add the location
            old = out[x.ats_job_id]
            if x.location and x.location not in old.location:
                out[x.ats_job_id] = old.model_copy(update={"location": f"{old.location}; {x.location}"[:500]})
            continue
        if len(out) >= opts.max_results:
            break
        out[x.ats_job_id] = x
    return list(out.values())


def describe(listing: Listing) -> str:
    """The posting page's description text (hidden text flagged), falling back to its JSON-LD description."""
    html, _ = get_text(listing.url)
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.styledText")
    if body is not None:
        text, hidden = html_to_text(str(body))
        if text:
            return mark_hidden(text, hidden)[:MAX_DESC]
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("description"):
            text, hidden = html_to_text(str(data["description"]))
            return mark_hidden(text, hidden)[:MAX_DESC]
    return ""


def board_of(url: str) -> str | None:
    """The slug of a {slug}.factorial.com or {slug}.factorialhr.com link, or None (custom domains aren't
    recognised)."""
    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    for domain in _DOMAINS:
        if host.endswith("." + domain):
            label = host[: -len(domain) - 1]
            if "." in label or label in _NOT_BOARDS:
                return None
            try:
                return _check(label)
            except ValueError:
                return None
    return None


BOARD = register_board(
    BoardFeed(
        ats="factorial",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="careers",
        terms="Public server-rendered career site (no key); one request per board, one per described posting; "
        "robots.txt allows /.",
        hosts=("factorial.com",),
    )
)
