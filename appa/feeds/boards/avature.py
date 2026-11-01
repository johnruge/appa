"""Avature career portals, through each portal's public XML sitemap plus its public job pages (no key).

A company's Avature site is {tenant}.avature.net, with job portals under paths like /careers (sometimes
behind a locale, /en_US/careers). The board slug is the tenant ("bloomberg"), or "tenant:portal" for a
portal not named "careers" ("bloomberg:AIResearchInternship").
GET https://{tenant}.avature.net/{portal}/sitemap_index.xml points at one sitemap per locale
(https://{host}/[en_US/]{portal}/sitemap.xml); we read one of them (en_US first) and keep its
<loc>.../{portal}/JobDetail/{Title-Slug}/{jobId}</loc> rows. Title comes from the slug, the ID from the last
segment, posted_at from <lastmod> (the posting's last update). Some tenants list their custom domain
(apply.deloitte.com) in the sitemap; we keep only the path and link the posting on {tenant}.avature.net,
which redirects to the company's page. The company name is read once per fetch from the first job page:
og:site_name, else the JSON-LD JobPosting's hiringOrganization.name, else the tenant.
Some tenants serve the sitemap empty, some only now and then (deloitteus), so an empty one is retried once
after a pause. If it is still empty we use the HTML search, /{portal}/SearchJobs?search={query}&jobOffset=N,
one paged search per opts.queries (all jobs when there are none), deduped by job ID. The tenant fixes the
page size (often 6-12), so each search is capped at MAX_PAGES and a warning is logged when that cap cuts
it short. Search rows take the title from the link text, plus the location and "Posted MM/DD/YYYY" date
where the tenant's markup shows them (.list-item-location / .list-item-posted); on tenants without a posted
date, opts.since cannot filter search rows. describe() reads one job page: its label/value fields
(Location, Ref #...) and rich-text description.
robots.txt allows /careers (except *qtvc= URLs) and lists the sitemap index; a portal other than careers is
used only if the tenant's robots.txt has an "Allow: /{portal}" line (Bloomberg ends with "Disallow: /").
Some tenants (Delta) sit behind an AWS WAF bot challenge (HTTP 202, empty body); we do not try to get past
it and raise instead.
The RSS feed at /{portal}/SearchJobs/feed/ returns only 20 items in no useful order, so it isn't used.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_bytes, get_text
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

_TENANT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")  # one DNS label
_PORTAL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
_LOCALE = r"(?:[a-z]{2}_[A-Z]{2}/)?"
_JOB = re.compile(rf"^/({_LOCALE})([A-Za-z][A-Za-z0-9_-]{{0,49}})/JobDetail/([^/?#\s]{{1,400}})/(\d{{1,12}})/?$")
_BOARD = re.compile(
    rf"^/{_LOCALE}([A-Za-z][A-Za-z0-9_-]{{0,49}})(?:/?|/SearchJobs(?:/.*)?|/JobDetail/[^/]+/\d{{1,12}}/?)$"
)
_SITEMAP = re.compile(rf"^/({_LOCALE})([A-Za-z][A-Za-z0-9_-]{{0,49}})/sitemap\.xml$")
_LOCALE_ONLY = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
_CLEARANCE = re.compile(r"clearance[\s-]+required|citizenship[\s-]+required|\bts[\s/-]*sci\b|polygraph", re.I)
_INTERN = re.compile(r"\bintern(ship)?\b", re.I)
_POSTED = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
MAX_PAGES = 50
MAX_DESC = 100_000
PAUSE = 0.5
RETRY_PAUSE = 3.0


def _parts(slug: str) -> tuple[str, str]:
    """(tenant, portal) of a validated slug; ValueError on anything unsafe."""
    if not isinstance(slug, str) or slug.count(":") > 1:
        raise ValueError(f"bad avature slug: {slug!r}")
    tenant, _, portal = slug.partition(":")
    portal = portal or "careers"
    if (
        not _TENANT.fullmatch(tenant)
        or tenant == "www"
        or not _PORTAL.fullmatch(portal)
        or _LOCALE_ONLY.fullmatch(portal)
    ):
        raise ValueError(f"bad avature slug: {slug!r}")
    return tenant, portal


def _base(slug: str) -> tuple[str, str]:
    """('https://{tenant}.avature.net', portal) for a validated slug."""
    tenant, portal = _parts(slug)
    return f"https://{quote(tenant, safe='')}.avature.net", quote(portal, safe="")


def _check_portal(base: str, portal: str) -> None:
    """RuntimeError unless the tenant's robots.txt allows this (non-careers) portal."""
    if portal == "careers":
        return
    text, _ = get_text(f"{base}/robots.txt")
    lines = {" ".join(line.split()) for line in text.splitlines()}
    if f"Allow: /{portal}" not in lines:
        raise RuntimeError(f"avature robots.txt does not allow the {portal} portal on {urlparse(base).hostname}")


def _day(v: str) -> date | None:
    """'2026-10-30' (or an ISO timestamp) -> date, or None."""
    try:
        return date.fromisoformat((v or "").strip()[:10])
    except ValueError:
        return None


def _title(seg: str) -> str:
    """'Senior-Software-Engineer-BPS-Monetization' -> 'Senior Software Engineer BPS Monetization'."""
    return " ".join(unquote(seg).replace("-", " ").split())


def _name(el) -> str:
    """An element's tag without its XML namespace."""
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _child(el, name: str) -> str:
    """Stripped text of the first child with this (namespace-free) tag, or ''."""
    return next(((c.text or "").strip() for c in el if _name(c) == name), "")


def _xml(url: str):
    """The parsed XML root at url; RuntimeError for an empty body (a bot challenge or an empty sitemap)."""
    body = get_bytes(url)
    if not body.strip():
        raise RuntimeError(f"avature sent an empty response for {urlparse(url).path} (bot challenge or no jobs)")
    return ElementTree.fromstring(body)


def _pick(locs: list[str], portal: str) -> str | None:
    """The path of the sitemap to read from an index: en_US first, then no locale, any English, any."""
    paths = [
        (m.group(1).rstrip("/"), urlparse(loc).path)
        for loc in locs
        if (m := _SITEMAP.match(urlparse(loc).path)) and m.group(2) == portal
    ]
    for want in ("en_US", "", "en_"):
        hit = next((p for loc, p in paths if loc == want or (want == "en_" and loc.startswith("en_"))), None)
        if hit:
            return hit
    return paths[0][1] if paths else None


def _sitemap(base: str, portal: str) -> list[tuple[str, str]]:
    """(loc, lastmod) of every <url> in the portal's sitemap, via its sitemap index."""
    root = _xml(f"{base}/{portal}/sitemap_index.xml")
    if _name(root) == "sitemapindex":
        path = _pick([_child(s, "loc") for s in root if _name(s) == "sitemap"], portal)
        if not path:
            return []
        time.sleep(PAUSE)
        root = _xml(base + path)
    return [(_child(u, "loc"), _child(u, "lastmod")) for u in root if _name(u) == "url"]


def _job_path(loc: str, portal: str) -> tuple[str, str, str] | None:
    """(canonical path, title slug, job id) of a JobDetail link on this portal, or None."""
    p = urlparse((loc or "").strip())
    m = _JOB.match(p.path)
    if p.scheme not in ("http", "https") or not m or m.group(2) != portal or p.query:
        return None
    locale, _, seg, jid = m.groups()
    seg = quote(unquote(seg), safe="-._~")
    return f"/{locale}{portal}/JobDetail/{seg}/{jid}", seg, jid


def _listing(row: tuple[str, str, str, str], base: str, portal: str, company: str) -> Listing:
    """One (loc, lastmod, title, location) row as a Listing (ValueError for non-job links)."""
    loc, lastmod, title, location = row
    hit = _job_path(loc, portal)
    if not hit:
        raise ValueError(f"not a job link on {portal}: {str(loc)[:80]!r}")
    path, seg, jid = hit
    title = " ".join((title or "").split())[:300] or _title(seg)
    hints = {}
    if _CLEARANCE.search(title):
        hints["citizenship_required"] = "yes"
    if _INTERN.search(title):
        hints["level"] = "intern"
    return Listing(
        company=company,
        title=title,
        url=base + path,
        location=" ".join(location.split())[:300],
        posted_at=_day(lastmod),
        ats="avature",
        ats_job_id=jid,
        source_ref=jid,
        hints=hints,
    )


def _posted(text: str) -> str:
    """'Posted 10/31/2026' -> '2026-10-31', or ''."""
    m = _POSTED.search(text or "")
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat() if m else ""
    except ValueError:
        return ""


def _results(html: str, final: str, base: str, portal: str) -> dict[str, list[str]]:
    """{job id: [loc, posted, title, location]} for the job links on one search page."""
    hosts = {urlparse(base).hostname, urlparse(final).hostname}  # the tenant, or the custom domain it sent us to
    ids: dict[str, list[str]] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        loc = urljoin(final, a["href"])
        hit = _job_path(loc, portal) if urlparse(loc).hostname in hosts else None
        if not hit:
            continue
        row = ids.setdefault(hit[2], [loc, "", "", ""])
        if not row[2] and a.find_parent(["h2", "h3", "h4"]):
            row[2] = a.get_text(" ", strip=True)
        box = a.find_parent("article") or a.find_parent("li")
        if box and not row[1] and (el := box.select_one(".list-item-posted")):
            row[1] = _posted(el.get_text(" ", strip=True))
        if box and not row[3] and (el := box.select_one(".list-item-location")):
            row[3] = el.get_text(" ", strip=True)
    return ids


def _search(base: str, portal: str, cap: int, query: str = "") -> list[tuple[str, str, str, str]]:
    """(loc, posted, title, location) rows from one paged HTML search, following the tenant's page size."""
    out, seen, offset, new = [], set(), 0, []
    params = {"search": query} if query else {}
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE)
        html, final = get_text(
            f"{base}/{portal}/SearchJobs", params={**params, "jobRecordsPerPage": "50", "jobOffset": str(offset)}
        )
        if page == 0 and not html.strip():
            raise RuntimeError(f"avature sent an empty search page for {portal} (bot challenge)")
        ids = _results(html, final, base, portal)
        new = [tuple(row) for jid, row in ids.items() if jid not in seen]
        seen.update(ids)
        out += new
        if not new or len(out) >= cap:
            return out
        offset += len(ids)
    log.warning(
        "avature search %r on %s/%s stopped at %d pages with %d jobs; more may exist",
        query,
        base,
        portal,
        MAX_PAGES,
        len(out),
    )
    return out


def _sitemap_rows(base: str, portal: str) -> list[tuple[str, str, str, str]]:
    """Job rows from the sitemap, retried once after a pause when it is empty or unreadable."""
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_PAUSE)
        try:
            rows = [(loc, mod, "", "") for loc, mod in _sitemap(base, portal) if _job_path(loc, portal)]
        except (RuntimeError, ElementTree.ParseError) as e:
            log.debug("avature sitemap unusable on %s/%s: %s", base, portal, e)
            rows = []
        if rows:
            return rows
    return []


def _page(base: str, path: str) -> BeautifulSoup:
    """A job page, parsed."""
    html, _ = get_text(base + path)
    return BeautifulSoup(html, "html.parser")


def _ld_org(soup: BeautifulSoup) -> str:
    """hiringOrganization.name from the page's JSON-LD blocks, or ''."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            org = item.get("hiringOrganization") if isinstance(item, dict) else None
            name = org.get("name") if isinstance(org, dict) else None
            if isinstance(name, str) and name.strip():
                return " ".join(name.split())
    return ""


def _company(base: str, path: str, tenant: str) -> str:
    """The og:site_name of one job page, else its JSON-LD hiringOrganization.name, else the tenant name."""
    try:
        soup = _page(base, path)
        meta = soup.find("meta", attrs={"property": "og:site_name"})
        name = (meta.get("content") or "").strip() if meta else ""
        name = name or _ld_org(soup)
        if name:
            return name[:300]
    except Exception as e:  # httpx errors (a job closed since the sitemap was built); the name is optional
        log.debug("avature company lookup failed on %s: %s", base, e)
    return tenant


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this portal (sitemap, else one HTML search per query); describe() fetches the text."""
    opts = opts or FeedOptions()
    tenant, _ = _parts(slug)
    base, portal = _base(slug)
    _check_portal(base, portal)
    rows = _sitemap_rows(base, portal)
    if not rows:
        queries = [q for q in dict.fromkeys(q.strip() for q in opts.queries) if q] or [""]
        for i, query in enumerate(queries):
            if i:
                time.sleep(PAUSE)
            rows += _search(base, portal, opts.max_results, query)
    if not rows:
        return []
    time.sleep(PAUSE)
    company = _company(base, _job_path(rows[0][0], portal)[0], tenant)
    out, seen = [], set()
    for row in rows:
        try:
            x = _listing(row, base, portal, company)
        except ValueError as e:
            log.debug("skipping avature row on %s: %s", base, e)
            continue
        if (opts.since and x.posted_at and x.posted_at < opts.since) or x.ats_job_id in seen:
            continue
        seen.add(x.ats_job_id)
        out.append(x)
        if len(out) >= opts.max_results:
            break
    return out


def describe(listing: Listing) -> str:
    """The posting's text from its job page: labelled fields on top, then the description."""
    slug = board_of(listing.url)
    hit = _job_path(listing.url, _parts(slug)[1]) if slug else None
    if not hit:
        raise ValueError(f"not an avature job link: {listing.url[:80]!r}")
    base, _ = _base(slug)
    soup = _page(base, hit[0])
    head, hidden = [], []
    for f in soup.select(".article__content__view__field"):
        label, value = (
            f.select_one(".article__content__view__field__label"),
            f.select_one(".article__content__view__field__value"),
        )
        if label and value and label.get_text(strip=True):
            name, _ = html_to_text(str(label))
            val, hid = html_to_text(str(value))
            hidden += hid
            if val:
                head.append(f"{' '.join(name.split()).rstrip(':')}: {' '.join(val.split())}")
            f.decompose()
    blocks = soup.select(".article--details") or soup.select("main") or ([soup.body] if soup.body else [soup])
    text, more = html_to_text("".join(str(b) for b in blocks))
    hidden += more
    out = "\n".join(head[:30]) + "\n\n" + mark_hidden(text, hidden)
    return out.replace("\xa0", " ").strip()[:MAX_DESC]


def board_of(url: str) -> str | None:
    """The slug of a {tenant}.avature.net portal, JobDetail or SearchJobs link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    m = _BOARD.match(p.path)
    if p.scheme not in ("http", "https") or not host.endswith(".avature.net") or not m:
        return None
    tenant, portal = host[: -len(".avature.net")], m.group(1)
    slug = tenant if portal == "careers" else f"{tenant}:{portal}"
    try:
        _parts(slug)
    except ValueError:
        return None
    return slug


BOARD = register_board(
    BoardFeed(
        ats="avature",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="bloomberg",
        terms="Public sitemap index (listed in robots.txt, which allows /careers except *qtvc= URLs) and public job "
        "pages; two sitemap requests (retried once if empty) plus one job page per fetch, else at most "
        "MAX_PAGES search pages per query; one job page per describe(). Other portals only when robots.txt "
        "allows them. Tenants behind a bot challenge are skipped, not bypassed.",
        hosts=("avature.net",),
    )
)
