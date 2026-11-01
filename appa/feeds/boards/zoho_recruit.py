"""Zoho Recruit job boards, read from the public careers page (no key, no API).

GET https://{sub}.zohorecruit.{tld}/jobs/{Portal} (Portal is usually "Careers") returns a ~2MB page whose
data sits in hidden inputs: id="jobs" holds the whole board as an HTML-escaped JSON array, id="meta" holds
org_info.company_name. Each job has id, Posting_Title / Job_Opening_Name, City, State, Country, Job_Type,
Remote_Job, Publish, Is_Locked and, per tenant, Date_Opened (ISO date), Department_Name and Job_Description
(posting text; some tenants cut it to a ~150 character teaser). Unpublished or locked jobs (filled
positions) are skipped. The human posting page is .../jobs/{Portal}/{id}. Zoho escapes '<' but not '>' in
the values, so the page is read with a real HTML parser, not a regex. Unknown tenants or portals answer 200
with a small page that has no jobs input, treated as "no postings". One request per board, no paging.

Slugs: "delsys" means delsys.zohorecruit.com/jobs/Careers; other data centres or portals use
"{sub}:{tld}:{Portal}", e.g. "acme:eu:Careers" or "acme:com:Jobs".
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

TLDS = ("com", "eu", "in", "com.au", "jp", "ca", "sa", "uk", "com.cn")  # Zoho Recruit data centres
_SUB = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")  # one DNS label
_PORTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_ID = re.compile(r"^[0-9]{6,30}$")
_NOT_BOARDS = {"www", "help", "api", "accounts", "static", "css", "js", "img", "blog", "support", "recruit"}
_TYPES = {
    "full time": "full_time",
    "part time": "part_time",
    "internship/co-op": "internship",
    "internship": "internship",
    "contract": "contract",
    "temporary": "temporary",
    "freelance": "freelance",
    "seasonal": "seasonal",
    "contract to hire": "contract_to_hire",
}
MAX_DESC = 100_000


def _parts(slug: str) -> tuple[str, str, str]:
    """(sub, tld, portal) of a slug, if every part is safe; else ValueError."""
    if not isinstance(slug, str):
        raise ValueError("bad zoho_recruit slug")
    bits = slug.split(":")
    if len(bits) > 3:
        raise ValueError(f"bad zoho_recruit slug: {slug!r}")
    sub, tld, portal = (bits + ["com", "Careers"][len(bits) - 1 :])[:3]
    sub, tld = sub.lower(), tld.lower()
    if not _SUB.fullmatch(sub) or sub in _NOT_BOARDS or tld not in TLDS or not _PORTAL.fullmatch(portal):
        raise ValueError(f"bad zoho_recruit slug: {slug!r}")
    return sub, tld, portal


def _slug(sub: str, tld: str, portal: str) -> str:
    """The canonical slug: just the subdomain for the usual .com/Careers board."""
    return sub if tld == "com" and portal.lower() == "careers" else f"{sub}:{tld}:{portal}"


def page_url(slug: str) -> str:
    """The careers page URL for a board."""
    sub, tld, portal = _parts(slug)
    return f"https://{quote(sub, safe='')}.zohorecruit.{tld}/jobs/{quote(portal, safe='')}"


class _Inputs(HTMLParser):
    """Collects the values of <input id="jobs"> and <input id="meta"> (attribute values come unescaped)."""

    def __init__(self):
        """Start with nothing found."""
        super().__init__()
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        """Keep the value of the first jobs/meta input."""
        a = dict(attrs)
        if tag == "input" and a.get("id") in ("jobs", "meta") and a["id"] not in self.found:
            self.found[a["id"]] = a.get("value") or ""


def _read(page: str) -> tuple[list, dict]:
    """(jobs, meta) from a careers page; ([], {}) when it has no (or unreadable) jobs data."""
    p = _Inputs()
    p.feed(page)
    p.close()
    try:
        jobs = json.loads(p.found.get("jobs") or "[]")
    except ValueError:
        jobs = []
    try:
        meta = json.loads(p.found.get("meta") or "{}")
    except ValueError:
        meta = {}
    return (jobs if isinstance(jobs, list) else []), (meta if isinstance(meta, dict) else {})


def _company(meta: dict, sub: str) -> str:
    """The company name the portal shows, else the subdomain."""
    org = meta.get("org_info") if isinstance(meta.get("org_info"), dict) else {}
    name = str(org.get("company_name") or "").strip()
    return name if name and not org.get("hide_company_name") else sub


def _day(v) -> date | None:
    """'2026-09-23' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _text(v) -> str:
    """A field as one trimmed string ('' for null; a lookup like {name, id} gives its name)."""
    if isinstance(v, dict):
        v = v.get("name")
    return " ".join(str(v).split()) if v not in (None, "", "-None-") else ""


def _description(raw) -> str:
    """The posting text (HTML or plain), hidden text flagged."""
    text, hidden = html_to_text(str(raw or ""))
    return mark_hidden(text, hidden)[:MAX_DESC]


def _listing(j: dict, sub: str, tld: str, portal: str, company: str) -> Listing:
    """One job as a Listing (ValueError when it is unpublished, locked, or a required field is bad)."""
    if not isinstance(j, dict) or j.get("Publish") is False or j.get("Is_Locked") is True:
        raise ValueError("not a live job")
    jid = str(j.get("id") or "")
    if not _ID.fullmatch(jid):
        raise ValueError("bad id")
    place = ", ".join(x for x in (_text(j.get(k)) for k in ("City", "State", "Country")) if x)
    remote = j.get("Remote_Job") is True
    location = (f"Remote ({place})" if place else "Remote") if remote else place
    jtype = _text(j.get("Job_Type"))
    hints = {
        "remote": "yes" if remote else "",
        "country": _text(j.get("Country")),
        "employment_type": _TYPES.get(jtype.lower(), jtype.lower().replace(" ", "_")),
        "category": _text(j.get("Department_Name")),
        "salary": _text(j.get("Salary")),
    }
    return Listing(
        company=company,
        title=_text(j.get("Posting_Title") or j.get("Job_Opening_Name")),
        url=f"https://{sub}.zohorecruit.{tld}/jobs/{quote(portal, safe='')}/{jid}",
        location=location[:500],
        posted_at=_day(j.get("Date_Opened")),
        description=_description(j.get("Job_Description")),
        ats="zoho_recruit",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every published job on this company's Zoho Recruit portal, with descriptions when the tenant shows them."""
    opts = opts or FeedOptions()
    sub, tld, portal = _parts(slug)
    jobs, meta = _read(get_bytes(page_url(slug)).decode("utf-8", errors="replace"))
    company = _company(meta, sub)
    out, seen = [], set()
    for j in jobs:
        try:
            x = _listing(j, sub, tld, portal, company)
        except ValueError as e:
            log.debug("skipping zoho_recruit job on %s: %s", slug, e)
            continue
        if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def board_of(url: str) -> str | None:
    """The board slug of a {sub}.zohorecruit.{tld}/jobs/{Portal}[/{id}/...] link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    tld = next((t for t in sorted(TLDS, key=len, reverse=True) if host.endswith(".zohorecruit." + t)), None)
    if not tld:
        return None
    sub = host[: -len(".zohorecruit." + tld)]
    bits = p.path.strip("/").split("/")
    if "." in sub or len(bits) < 2 or bits[0] != "jobs":
        return None
    try:
        return _slug(*_parts(f"{sub}:{tld}:{bits[1]}"))
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="zoho_recruit",
        fetch=fetch,
        board_of=board_of,
        example="delsys",
        terms="Public careers page (no key); robots.txt allows /jobs/. One ~2MB page per board, so poll known "
        "boards only and not more than daily.",
        hosts=tuple(f"zohorecruit.{t}" for t in TLDS),
    )
)
