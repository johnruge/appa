"""iCIMS career portals, through each portal's public XML sitemap plus the JSON-LD on job pages (no key).

A company's iCIMS portal lives on its own host under icims.com, e.g. careers-gdms.icims.com or
careers-americas.icims.com (Atlassian); the board slug is that host's first label ("careers-gdms").
GET https://{slug}.icims.com/sitemap.xml lists every open job as
<url><loc>https://{host}/jobs/{id}/{title-slug}/job</loc><lastmod>ISO</lastmod></url> (plus /jobs/intro).
The sitemap has no company, location or text, so the title comes from the URL's title slug (URL-decoded,
'-' -> ' ') and posted_at is the lastmod date (the posting's last update). The company name is read once
per fetch from the first job page. Full text comes from describe(), one request to
https://{host}/jobs/{id}/job?in_iframe=1, whose <script type="application/ld+json"> holds a schema.org
JobPosting (title, description, datePosted, employmentType, hiringOrganization, jobLocation).
robots.txt allows /jobs/* (except referral/login/candidate/reminder) and points crawlers at the sitemap.
A dead portal answers 404 "gone"; a closed job page answers 410. Large portals may serve a sitemap index,
whose child sitemaps on the same host are followed (capped, with a pause between them).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from urllib.parse import quote, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")  # one DNS label like "careers-gdms" (always hyphenated)
_JOB_PATH = re.compile(r"^/jobs/(\d{1,12})/([^/?#\s]{1,400})/job/?$")
_BOARD_PATH = re.compile(r"^/jobs/(?:\d{1,12}/[^/]+/job|search|intro)/?$")
_CLEARANCE = re.compile(r"clearance[\s-]+required|citizenship[\s-]+required|\bts/?sci\b", re.I)
_TYPES = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACTOR": "contract",
    "TEMPORARY": "temporary",
    "INTERN": "internship",
    "PER_DIEM": "per_diem",
}
MAX_SITEMAPS = 50
MAX_DESC = 100_000
PAUSE = 0.5


def _check(slug: str) -> str:
    """The slug, if it is a safe iCIMS portal host label; else ValueError."""
    if not isinstance(slug, str) or len(slug) > 63 or not _SLUG.fullmatch(slug.lower()):
        raise ValueError(f"bad icims slug: {slug!r}")
    return slug.lower()


def _host(slug: str) -> str:
    """The portal host for a validated slug."""
    return f"{quote(_check(slug), safe='')}.icims.com"


def _day(v: str) -> date | None:
    """'2026-10-30T20:36:52-04:00' -> date, or None."""
    try:
        return date.fromisoformat((v or "").strip()[:10])
    except ValueError:
        return None


def _title(seg: str) -> str:
    """'software-engineer-%e2%80%93-entry-level' -> 'software engineer – entry level'."""
    return " ".join(unquote(seg).replace("-", " ").split())


def _listing(loc: str, lastmod: str, host: str, company: str) -> Listing:
    """One sitemap <url> as a Listing (ValueError for non-job or foreign links)."""
    p = urlparse(loc.strip())
    m = _JOB_PATH.match(p.path)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() != host or not m or p.query:
        raise ValueError(f"not a job link on {host}: {loc[:80]!r}")
    jid, seg = m.groups()
    title = _title(seg)
    hints = {"citizenship_required": "yes"} if _CLEARANCE.search(title) else {}
    return Listing(
        company=company,
        title=title,
        url=f"https://{host}/jobs/{jid}/{seg}/job",
        posted_at=_day(lastmod),
        ats="icims",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def _name(el) -> str:
    """An element's tag without its XML namespace."""
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _child(el, name: str) -> str:
    """Stripped text of the first child with this (namespace-free) tag, or ''."""
    return next(((c.text or "").strip() for c in el if _name(c) == name), "")


def _urls(root) -> list[tuple[str, str]]:
    """(loc, lastmod) of every <url> in a urlset."""
    return [(_child(u, "loc"), _child(u, "lastmod")) for u in root if _name(u) == "url"]


def _sitemap(host: str) -> list[tuple[str, str]]:
    """Every (loc, lastmod) in the portal's sitemap, following a same-host sitemap index (capped)."""
    root = ElementTree.fromstring(get_bytes(f"https://{host}/sitemap.xml"))
    if _name(root) != "sitemapindex":
        return _urls(root)
    out = []
    children = [_child(s, "loc") for s in root if _name(s) == "sitemap"]
    for i, loc in enumerate(c for c in children if (urlparse(c).hostname or "").lower() == host):
        if i >= MAX_SITEMAPS:
            break
        if i:
            time.sleep(PAUSE)
        out += _urls(ElementTree.fromstring(get_bytes(f"https://{host}{urlparse(loc).path}")))
    return out


def _posting(host: str, jid: str) -> dict:
    """The schema.org JobPosting from a job page's JSON-LD ({} when the page has none)."""
    html, _ = get_text(f"https://{host}/jobs/{quote(jid, safe='')}/job", params={"in_iframe": "1"})
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d
    return {}


def _company(host: str, jid: str, slug: str) -> str:
    """The hiring organisation named on one job page, else a name made from the slug."""
    try:
        org = _posting(host, jid).get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()[:300]
    except httpx.HTTPStatusError as e:  # that job closed since the sitemap was built
        log.debug("icims company lookup failed on %s: %s", host, e)
    return slug.split("-", 1)[1]


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job in this portal's sitemap, titles only (describe() fetches a job's text)."""
    opts = opts or FeedOptions()
    host = _host(slug)
    rows = [(loc, mod, m.group(1)) for loc, mod in _sitemap(host) if (m := _JOB_PATH.match(urlparse(loc).path))]
    if not rows:
        return []
    company = _company(host, rows[0][2], _check(slug))
    out, seen = [], set()
    for loc, mod, _ in rows:
        try:
            x = _listing(loc, mod, host, company)
        except ValueError as e:
            log.debug("skipping icims row on %s: %s", host, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def _place(p: dict) -> str:
    """'Pittsburgh, PA, US; ...' from jobLocation addresses."""
    locs = p.get("jobLocation")
    names = []
    for loc in locs if isinstance(locs, list) else [locs]:
        a = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(a, dict):
            parts = [a.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            names.append(", ".join(str(x) for x in parts if x and x != "UNAVAILABLE"))
    return "; ".join(dict.fromkeys(n for n in names if n))


def describe(listing: Listing) -> str:
    """The posting's text from its job page JSON-LD, with location, type and dates on top."""
    p = urlparse(listing.url)
    slug, m = board_of(listing.url), _JOB_PATH.match(p.path)
    if not slug or not m:
        raise ValueError(f"not an icims job link: {listing.url[:80]!r}")
    d = _posting(_host(slug), m.group(1))
    text, hidden = html_to_text(str(d.get("description") or ""))
    etype = d.get("employmentType")
    etype = ", ".join(map(str, etype)) if isinstance(etype, list) else str(etype or "")
    org = d.get("hiringOrganization") if isinstance(d.get("hiringOrganization"), dict) else {}
    head = [
        f"{k}: {v}"
        for k, v in (
            ("Title", d.get("title")),
            ("Company", org.get("name")),
            ("Location", _place(d)),
            ("Remote", "yes" if d.get("jobLocationType") == "TELECOMMUTE" else ""),
            ("Employment type", "" if etype == "OTHER" else _TYPES.get(etype, etype)),
            ("Category", d.get("occupationalCategory")),
            ("Posted", str(d.get("datePosted") or "")[:10]),
            ("Valid through", str(d.get("validThrough") or "")[:10]),
        )
        if v
    ]
    return ("\n".join(head) + "\n\n" + mark_hidden(text, hidden)).strip()[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The portal slug of a {slug}.icims.com/jobs/... job, search or intro link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or not host.endswith(".icims.com") or not _BOARD_PATH.match(p.path):
        return None
    label = host[: -len(".icims.com")]
    try:
        return _check(label) if "." not in label else None
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="icims",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="careers-gdms",
        terms="Public sitemap.xml (robots.txt points crawlers to it) and public job pages (robots allows /jobs/*); "
        "one sitemap request plus one job page per fetch, and one job page per describe().",
        hosts=("icims.com",),
    )
)
