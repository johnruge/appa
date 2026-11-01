"""Radancy (TalentBrew) career sites, through each site's public XML sitemap plus the JSON-LD on job pages (no key).

board_of matches only the TalentBrew URL shapes /job/{city}/{title}/{orgId 1-6 digits}/{jobId 7+ digits} and
/category|location/{slug}/{orgId}/{id}/{n}, and never big job boards (Indeed, LinkedIn, Glassdoor, ...).
TalentBrew hosts a company's career site on the company's own domain (jobs.intuit.com, www.capitalonecareers.com,
careers.unitedhealthgroup.com), with assets on tbcdn.talentbrew.com. The board slug is that career host.
GET https://{host}/sitemap.xml lists every open job as
<url><loc>https://{host}/job/{city-slug}/{title-slug}/{orgId}/{jobId}</loc>...</url> (plus home, /category/ and
/location/ pages, which are skipped). The sitemap's lastmod is the time it was generated, not a posting date, so
posted_at is left empty. Titles and cities come from the URL slugs, so they are lower-case and lose punctuation
('staff ai scientist fintech risk ai ml' for 'Staff AI Scientist (Fintech Risk AI/ML)'), and there is no country, so
country / us_only filters only work on the describe() text, which has the real title and full location. The company
is read once per fetch from the first job page, which must also carry a TalentBrew marker (tbcdn.talentbrew.com
assets or the career-site-full-domain meta tag), else the host is not TalentBrew and fetch returns []. Postings are
deduped on orgId + jobId (one site can carry several orgIds). Full text comes from
describe(): one request to the job page, whose <script type="application/ld+json"> holds a schema.org JobPosting
(title, description, datePosted, employmentType, hiringOrganization, jobLocation).
robots.txt on these sites disallows only /search-jobs/ (the XHR search), so that endpoint is never used. Some
companies' own site terms forbid robots on their career site (Boeing's do); those hosts are refused.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_HOST = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SEG = r"[A-Za-z0-9%._~-]{1,400}"
_JOB_PATH = re.compile(rf"^/job/({_SEG})/({_SEG})/(\d{{1,6}})/(\d{{7,20}})/?$")
_BOARD_PATH = re.compile(
    rf"^/(?:job/{_SEG}/{_SEG}/\d{{1,6}}/\d{{7,20}}"
    rf"|(?:category|location)/{_SEG}/\d{{1,6}}/\d{{1,20}}/\d{{1,6}})/?$"
)
_REFUSED = ("boeing.com",)  # site terms forbid "data mining, robots, or similar data gathering"
_NOT_TALENTBREW = (
    "indeed.com",
    "linkedin.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "simplyhired.com",
    "careerbuilder.com",
    "dice.com",
    "google.com",
    "github.com",
    "lever.co",
    "greenhouse.io",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "icims.com",
    "ashbyhq.com",
    "talentbrew.com",
)
_MARKER = re.compile(r"tbcdn\.talentbrew\.com|<meta[^>]+career-site-full-domain", re.I)
MAX_PROBES = 3
_CLEARANCE = re.compile(r"clearance[\s-]+required|citizenship[\s-]+required|\bts/?sci\b", re.I)
_TYPES = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACTOR": "contract",
    "CONTRACT": "contract",
    "TEMPORARY": "temporary",
    "INTERN": "internship",
    "INTERNSHIP": "internship",
    "PER_DIEM": "per_diem",
}
MAX_SITEMAPS = 50
MAX_DESC = 100_000
PAUSE = 0.5


def _under(host: str, domains: tuple[str, ...]) -> bool:
    """True when host is one of these domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _refused(host: str) -> bool:
    """True for career hosts whose owner's terms forbid automated collection."""
    return _under(host, _REFUSED)


def _check(slug: str) -> str:
    """The slug, if it is a plain public hostname whose terms allow this; else ValueError."""
    host = slug.lower().rstrip(".") if isinstance(slug, str) else ""
    if not _HOST.fullmatch(host) or _under(host, _NOT_TALENTBREW):
        raise ValueError(f"bad talentbrew slug: {slug!r}")
    if _refused(host):
        raise ValueError(f"{host}: the site's terms forbid automated collection")
    return host


def _title(seg: str) -> str:
    """'software-engineer-2-mid-market-verticals' -> 'software engineer 2 mid market verticals'."""
    return " ".join(unquote(seg).replace("-", " ").split())


def _listing(loc: str, host: str, company: str) -> Listing:
    """One sitemap job <loc> as a Listing (ValueError for non-job or foreign links)."""
    p = urlparse(loc.strip())
    m = _JOB_PATH.match(p.path)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() != host or not m or p.query:
        raise ValueError(f"not a job link on {host}: {loc[:80]!r}")
    city, seg, org, jid = m.groups()
    title = _title(seg)
    hints = {"citizenship_required": "yes"} if _CLEARANCE.search(title) else {}
    return Listing(
        company=company,
        title=title,
        url=f"https://{host}/job/{city}/{seg}/{org}/{jid}",
        location=_title(city).title(),
        ats="radancy_talentbrew",
        ats_job_id=jid,
        source_ref=f"{org}:{jid}",
        hints=hints,
    )


def _name(el) -> str:
    """An element's tag without its XML namespace."""
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _child(el, name: str) -> str:
    """Stripped text of the first child with this (namespace-free) tag, or ''."""
    return next(((c.text or "").strip() for c in el if _name(c) == name), "")


def _locs(root) -> list[str]:
    """The <loc> of every <url> in a urlset."""
    return [_child(u, "loc") for u in root if _name(u) == "url"]


def _sitemap(host: str) -> list[str]:
    """Every <loc> in the site's sitemap, following a same-host sitemap index (capped)."""
    root = ElementTree.fromstring(get_bytes(f"https://{host}/sitemap.xml"))
    if _name(root) != "sitemapindex":
        return _locs(root)
    out = []
    children = [_child(s, "loc") for s in root if _name(s) == "sitemap"]
    for i, loc in enumerate(c for c in children if (urlparse(c).hostname or "").lower() == host):
        if i >= MAX_SITEMAPS:
            break
        if i:
            time.sleep(PAUSE)
        out += _locs(ElementTree.fromstring(get_bytes(f"https://{host}{urlparse(loc).path}")))
    return out


def _posting(url: str) -> dict:
    """The schema.org JobPosting from a job page's JSON-LD ({} when the page has none)."""
    return _ld(get_text(url)[0])


def _ld(html: str) -> dict:
    """The schema.org JobPosting in this page's JSON-LD ({} when there is none)."""
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d
    return {}


def _fallback_company(host: str) -> str:
    """A company name from the host: 'jobs.intuit.com' -> 'intuit'."""
    labels = [x for x in host.split(".")[:-1] if x not in ("www", "jobs", "careers", "career")]
    return labels[-1] if labels else host


def _company(urls: list[str], host: str) -> str | None:
    """The company from the first job page that loads (host-made if unnamed); None if the site is not TalentBrew."""
    for i, url in enumerate(urls[:MAX_PROBES]):
        if i:
            time.sleep(PAUSE)
        try:
            html, _ = get_text(url)
        except httpx.HTTPStatusError as e:  # that job closed since the sitemap was built
            log.debug("talentbrew job page failed on %s: %s", host, e)
            continue
        if not _MARKER.search(html):
            log.info("%s has no TalentBrew marker; skipping", host)
            return None
        org = _ld(html).get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else None
        return name.strip()[:300] if isinstance(name, str) and name.strip() else _fallback_company(host)
    log.info("no TalentBrew job page on %s loaded; skipping", host)
    return None


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job in this career site's sitemap, titles only (describe() fetches a job's text)."""
    opts = opts or FeedOptions()
    host = _check(slug)
    locs = [loc for loc in _sitemap(host) if _JOB_PATH.match(urlparse(loc.strip()).path)]
    if not locs:
        return []
    out, seen = [], set()
    for loc in locs:
        try:
            x = _listing(loc, host, "pending")
        except ValueError as e:
            log.debug("skipping talentbrew row on %s: %s", host, e)
            continue
        if x.source_ref not in seen:
            seen.add(x.source_ref)
            out.append(x)
        if len(out) >= opts.max_results:
            break
    company = _company([x.url for x in out], host) if out else None
    return [x.model_copy(update={"company": company}) for x in out] if company else []


def _place(p: dict) -> str:
    """'Mountain View, California, United States; ...' from jobLocation addresses."""
    locs = p.get("jobLocation")
    names = []
    for loc in locs if isinstance(locs, list) else [locs]:
        a = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(a, dict):
            parts = [a.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            names.append(", ".join(str(x) for x in parts if x))
    return "; ".join(dict.fromkeys(n for n in names if n))


def _etype(v) -> str:
    """'Full-Time' or ['FULL_TIME'] -> 'full_time'."""
    vals = v if isinstance(v, list) else [v]
    keys = [re.sub(r"[\s-]+", "_", str(x).strip()).upper() for x in vals if x]
    return ", ".join(_TYPES.get(k, k.lower()) for k in keys if k not in ("OTHER", "NONE"))


def describe(listing: Listing) -> str:
    """The posting's text from its job page JSON-LD, with location, type and dates on top."""
    host, m = board_of(listing.url), _JOB_PATH.match(urlparse(listing.url.strip()).path)
    if not host or not m:
        raise ValueError(f"not a talentbrew job link: {listing.url[:80]!r}")
    d = _posting(f"https://{host}/job/{'/'.join(m.groups())}")
    text, hidden = html_to_text(str(d.get("description") or ""))
    org = d.get("hiringOrganization") if isinstance(d.get("hiringOrganization"), dict) else {}
    head = [
        f"{k}: {v}"
        for k, v in (
            ("Title", d.get("title")),
            ("Company", org.get("name")),
            ("Location", _place(d)),
            ("Remote", "yes" if d.get("jobLocationType") == "TELECOMMUTE" else ""),
            ("Employment type", _etype(d.get("employmentType"))),
            ("Category", d.get("occupationalCategory")),
            ("Posted", str(d.get("datePosted") or "")[:10]),
            ("Valid through", str(d.get("validThrough") or "")[:10]),
        )
        if v
    ]
    return ("\n".join(head) + "\n\n" + mark_hidden(text, hidden)).strip()[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The career host of a TalentBrew job, category or location link (/job/{city}/{title}/{org}/{id}), or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or p.port not in (None, 80, 443) or not _BOARD_PATH.match(p.path):
        return None
    try:
        return _check(host)
    except ValueError:
        return None


BOARD = register_board(
    BoardFeed(
        ats="radancy_talentbrew",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="jobs.intuit.com",
        terms="Public sitemap.xml and job pages on each company's TalentBrew career site; robots.txt disallows only "
        "/search-jobs/, which is never called. One sitemap request plus one job page per fetch (up to 3 "
        "if the first ones closed), one job page per describe(). Hosts whose site terms forbid robots (boeing.com) "
        "are refused.",
        hosts=(),
    )
)
