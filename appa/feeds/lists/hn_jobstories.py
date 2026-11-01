"""Hacker News job stories: the "X (YC S19) Is Hiring ..." posts YC companies place on news.ycombinator.com/jobs.
These are not the monthly "Who is hiring?" comments (that is appa/feeds/lists/hn_whoishiring.py).

appa reads the official HN Firebase API (https://github.com/HackerNews/API, v0, no key, no stated rate limit):
GET /v0/jobstories.json lists up to ~200 item ids, newest first, and GET /v0/item/{id}.json returns each post
({id, by, time, title, type: "job", url?, text?}). Items are fetched about once a second and cached in-process,
so a later poll only fetches ids it hasn't seen; with opts.since the walk stops at the first older post.

The whole page is one global feed, so it registers as a list source ("hn_jobstories", polled by default; a
BoardFeed with one fixed slug would never be seeded or polled).
Each post becomes one Listing: the company comes from the title ("Wasmer (YC S19) Is Hiring ..."), the link is
the post's url, else the HN item (with the post's text as the description). Many posts link to Work at a
Startup or ycombinator.com job pages, which need a YC account to apply: those are marked ats="workatastartup"
(apply by hand), and the company's own Greenhouse/Lever/Ashby board is probed (slug = the YC company slug or
the lower-cased name). A probed posting replaces the YC link only when its title matches the post AND the board
is the same company (Greenhouse's company_name, or the company named in the Lever/Ashby posting text); otherwise
the board is added to hints["links"] so appa discovers and polls it. Only a 404 is cached as "no board"; other
errors are retried on the next fetch. Posts that link straight to a Greenhouse/Lever/Ashby posting get that ats;
posts that link to such a board without a posting add the board to hints["links"]. describe() fetches the text of
any listing whose link is a Greenhouse/Lever/Ashby posting (one request). Work at a Startup links answer 406 to
non-browser clients, so they can only be checked in a real browser.
Identity: the link's job ID when it has one, the YC job ID for ycombinator.com links when it carries a digit
(appa's job-ID rules need one), else the HN item id.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import quote, urlparse

import httpx

from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.ats import SINGLE, AtsRef, parse_url
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://hacker-news.firebaseio.com/v0"
HN_ITEM = "https://news.ycombinator.com/item?id="
PAUSE = 1.0  # stay under ~1 request/second
MAX_ITEMS = 200  # jobstories.json holds at most ~200 ids
MAX_PROBES = 15  # companies whose ATS boards are probed per fetch
CACHE_SIZE = 1000
MAX_DESC = 100_000
MAX_LINKS = 300  # hints["links"] cap, in characters
OWNER_TEXT = 20_000  # characters of each probed posting's text kept to check the company

_ATS_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_YC_TITLE = re.compile(r"^(.*?) \((YC [A-Z]\d+)\)")
_HIRING = re.compile(r"^(.*?)\s+is hiring\b\s*(?:[-–—:]\s*)?(?:an?\s+)?(.*)$", re.I)
_YC_JOB = re.compile(r"^/companies/([a-z0-9-]{1,80})/jobs/([A-Za-z0-9]{4,20})(?:-|/|$)")
_HREF = re.compile(r'href="([^"]+)"', re.I)
_WAAS = ("workatastartup.com",)
_ATS_HOSTS = {"greenhouse": ("greenhouse.io",), "lever": ("lever.co",), "ashby": ("ashbyhq.com",)}
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "the", "gmbh"}
_PROBES = (  # (ats, board API, how to read its jobs as (title, url, id, text naming the company))
    (
        "greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
        lambda d: [
            (j.get("title"), j.get("absolute_url"), j.get("id"), j.get("company_name"))
            for j in (d or {}).get("jobs") or []
        ],
    ),
    (
        "lever",
        "https://api.lever.co/v0/postings/{}?mode=json",
        lambda d: [
            (
                j.get("text"),
                j.get("hostedUrl"),
                j.get("id"),
                " ".join(str(j.get(k) or "") for k in ("openingPlain", "descriptionPlain", "additionalPlain")),
            )
            for j in d or []
        ],
    ),
    (
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{}",
        lambda d: [
            (j.get("title"), j.get("jobUrl"), j.get("id"), j.get("descriptionPlain"))
            for j in (d or {}).get("jobs") or []
        ],
    ),
)
_BOARD_URL = {
    "greenhouse": "https://job-boards.greenhouse.io/{}",
    "lever": "https://jobs.lever.co/{}",
    "ashby": "https://jobs.ashbyhq.com/{}",
}

_items: dict[int, dict] = {}  # item id -> item, across polls in one process
_boards: dict[str, tuple[str, str, list] | None] = {}  # ATS slug -> (ats, slug, jobs) or None (404s)


def _host(url: str) -> str:
    """Lower-case host of a link ('' if it has none)."""
    try:
        return (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return ""


def _under(host: str, domains: tuple[str, ...]) -> bool:
    """True if host is one of the domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _yc_job(url: str) -> tuple[str, str] | None:
    """(company slug, job id) of a ycombinator.com/companies/{slug}/jobs/{id}-... link, else None."""
    if not _under(_host(url), ("ycombinator.com",)):
        return None
    m = _YC_JOB.match(urlparse(url.strip()).path)
    return (m.group(1), m.group(2)) if m else None


def _ats_ref(url: str) -> AtsRef | None:
    """parse_url(url) when the link's host really is that ATS's domain and the slug/job id are well-formed, else
    None."""
    ref = parse_url(url) if url else None
    if not ref or ref.ats not in SINGLE or not _under(_host(url), _ATS_HOSTS.get(ref.ats, ())):
        return None
    if not _SLUG.fullmatch(ref.slug or "") or (ref.job_id and not _SLUG.fullmatch(ref.job_id)):
        return None
    return ref


def _needs_yc_login(url: str) -> bool:
    """True for Work at a Startup and ycombinator.com job links (applying needs a YC account)."""
    return _under(_host(url), _WAAS) or _yc_job(url) is not None


def _date(ts) -> date | None:
    """A unix timestamp as a UTC date (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _words(s: str) -> str:
    """Lower-case alphanumeric words joined by single spaces, for title matching."""
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def _company_role(title: str) -> tuple[str, str]:
    """(company, role text) from 'Name (YC S19) Is Hiring a Role'; company '' when the title names none."""
    m = _YC_TITLE.match(title)
    h = _HIRING.match(title)
    company = m.group(1) if m else h.group(1) if h else ""
    role = h.group(2) if h else ""
    company = re.sub(r"\([^)]*\)", "", company).strip(" -–—:,|")
    return " ".join(company.split()), role.strip()


def _hints(title: str) -> dict[str, str]:
    """The facts the post's title states, under HINT_KEYS names."""
    t, hints = title.lower(), {}
    if "hybrid" in t:
        hints["remote"] = "hybrid"
    elif re.search(r"\bremote\b", t):
        hints["remote"] = "yes"
    if re.search(r"new[- ]?grads?\b", t):
        hints["level"] = "new_grad"
    elif re.search(r"\bintern(ship)?s?\b", t):
        hints["level"] = "intern"
    elif re.search(r"\b(junior|entry[- ]level)\b", t):
        hints["level"] = "entry"
    elif re.search(r"\b(senior|staff|principal)\b", t):
        hints["level"] = "senior"
    for kind, rx in (
        ("internship", r"\bintern(ship)?s?\b"),
        ("full_time", r"full[- ]?time"),
        ("contract", r"\bcontract\b"),
        ("part_time", r"part[- ]?time"),
    ):
        if re.search(rx, t):
            hints["employment_type"] = kind
            break
    return hints


def _item(iid: int) -> dict | None:
    """One HN item, from the cache or one request (None when the API returns null)."""
    if iid in _items:
        return _items[iid]
    data = get_json(f"{API}/item/{iid}.json")
    if not isinstance(data, dict):
        return None
    if len(_items) >= CACHE_SIZE:
        _items.pop(next(iter(_items)))
    _items[iid] = data
    return data


def _probe(slug: str) -> tuple[str, str, list] | None:
    """(ats, slug, [(title, url, id, owner text)]) of the first Greenhouse/Lever/Ashby board with this slug, else
    None; only "every ATS said 404" is cached, so a timeout or 5xx is retried on the next fetch."""
    if slug in _boards:
        return _boards[slug]
    found, all_404 = None, True
    for i, (ats, tmpl, read) in enumerate(_PROBES):
        if i:
            time.sleep(PAUSE)
        try:
            jobs = read(get_json(tmpl.format(quote(slug, safe=""))))
        except httpx.HTTPStatusError as e:
            all_404 &= e.response.status_code == 404
            log.debug("no %s board for %s: HTTP %s", ats, slug, e.response.status_code)
            continue
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
            all_404 = False  # transient or unexpected: don't remember it as missing
            log.debug("no %s board for %s: %s", ats, slug, type(e).__name__)
            continue
        found = (ats, slug, [(t, u, j, str(o or "")[:OWNER_TEXT]) for t, u, j, o in jobs])
        break
    if found or all_404:
        if len(_boards) >= CACHE_SIZE:
            _boards.pop(next(iter(_boards)))
        _boards[slug] = found
    return found


def _owned(board: tuple[str, str, list], owner: str, company: str) -> bool:
    """True if a probed posting is the HN company's own: Greenhouse's company name matches it, or the
    Lever/Ashby posting text names it."""
    want = _words(company)
    if not want:
        return False
    if board[0] == "greenhouse":
        return _name(owner) == _name(company)
    return f" {want} " in f" {_words(owner)} "


def _name(s: str) -> str:
    """A company name's words without legal suffixes ("Acme, Inc." -> "acme"), for exact comparison."""
    return " ".join(w for w in _words(s).split() if w not in _SUFFIXES)


def _add_links(hints: dict[str, str], *urls: str) -> dict[str, str]:
    """hints with urls appended to hints["links"] (deduplicated, capped at MAX_LINKS characters)."""
    links = list(dict.fromkeys([*hints.get("links", "").split(), *(u for u in urls if u)]))
    kept: list[str] = []
    for u in links:
        if len(" ".join([*kept, u])) > MAX_LINKS:
            break
        kept.append(u)
    return {**hints, "links": " ".join(kept)} if kept else hints


def _match(board: tuple[str, str, list], title: str, role: str) -> tuple[str, str] | None:
    """(url, id) of the board's posting whose title matches the HN post, else None."""
    hn, want = _words(title), _words(role)
    best = None
    for jt, url, jid, _ in board[2]:
        name = _words(jt)
        if len(name) < 6 or not (isinstance(url, str) and url.startswith("https://")) or jid in (None, ""):
            continue
        if (f" {name} " in f" {hn} " or name == want) and (best is None or len(name) > len(best[0])):
            best = (name, url, str(jid))
    return (best[1], best[2]) if best else None


def _hit(board: tuple[str, str, list], title: str, role: str, company: str) -> tuple[str, str] | None:
    """(url, id) of the board's posting that matches the HN post and is the HN company's own, else None."""
    hit = _match(board, title, role)
    if not hit:
        return None
    owner = next((o for _, u, j, o in board[2] if u == hit[0] and str(j) == hit[1]), "")
    return hit if _owned(board, owner, company) else None


def _listing(it: dict) -> tuple[Listing | None, str | None]:
    """(Listing or None for dead/deleted/malformed posts, the ATS slug to probe when it links a YC page)."""
    iid, title = it.get("id"), str(it.get("title") or "")
    if (
        it.get("type") != "job"
        or it.get("dead")
        or it.get("deleted")
        or not str(iid or "").isdigit()
        or not title.strip()
    ):
        return None, None
    company, _ = _company_role(title)
    url = str(it.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = ""
    description, links = "", []
    if it.get("text"):
        raw = str(it["text"])
        text, hidden = html_to_text(raw)
        description = mark_hidden(text, hidden)[:MAX_DESC]
        links = [
            u
            for u in (html.unescape(h).strip() for h in _HREF.findall(raw))
            if u.lower().startswith(("http://", "https://")) and len(u) <= 300
        ]
    if not company:
        company = _host(url).removeprefix("www.") if url else ""
    if not company:
        return None, None
    link = url or f"{HN_ITEM}{iid}"
    yc = _yc_job(link)
    waas = _needs_yc_login(link)
    hints = _add_links(_hints(title), *links)
    ref = None if waas else _ats_ref(link)
    if ref and not ref.job_id:  # a company board, not one posting: let appa poll it
        hints = _add_links(hints, _BOARD_URL[ref.ats].format(quote(ref.slug, safe="")))
    ats = "workatastartup" if waas else ref.ats if ref and ref.job_id else "unknown"
    yc_id = yc[1] if yc and re.search(r"\d", yc[1]) else None  # digit-free YC ids aren't valid job IDs
    job_id = None if job_key(link) else (yc_id or f"hn{iid}")
    probe = None
    if waas:
        probe = yc[0] if yc else re.sub(r"[^a-z0-9]", "", company.lower())
        probe = probe if _ATS_SLUG.fullmatch(probe or "") else None
    try:
        return Listing(
            company=company[:300],
            title=title[:500],
            url=link,
            posted_at=_date(it.get("time")),
            description=description,
            ats=ats,
            ats_job_id=job_id,
            source_ref=f"hn{iid}",
            hints=hints,
        ), probe
    except ValueError:
        return None, None


def _prefer_ats(x: Listing, board: tuple[str, str, list]) -> Listing:
    """The listing moved to the matching posting on the company's own ATS, or with the board in hints["links"]."""
    _, role = _company_role(x.title)
    hit = _hit(board, x.title, role, x.company)
    if not hit:
        board_url = _BOARD_URL[board[0]].format(quote(board[1], safe=""))
        return x.model_copy(update={"hints": _add_links(x.hints, board_url)})
    url, jid = hit
    return x.model_copy(
        update={"url": url, "ats": board[0], "ats_job_id": jid[:100], "hints": _add_links(x.hints, x.url[:MAX_LINKS])}
    )


def fetch(opts: FeedOptions | None = None) -> list[Listing]:
    """Every live HN job story (newest first), stopping at opts.since; YC-login posts get an ATS probe."""
    opts = opts or FeedOptions()
    ids = get_json(f"{API}/jobstories.json")
    if not isinstance(ids, list):
        raise ValueError("HN jobstories.json is not a list")
    out, seen, probes = [], set(), 0
    for raw in ids[:MAX_ITEMS]:
        if len(out) >= opts.max_results:
            break
        if not str(raw).isdigit():
            continue
        iid = int(raw)
        fresh = iid not in _items
        it = _item(iid)
        if fresh:
            time.sleep(PAUSE)
        if not it:
            continue
        x, probe = _listing(it)
        if x is None:
            continue
        if opts.since and x.posted_at and x.posted_at < opts.since:
            break  # ids are newest first: the rest are older
        if probe and (probe in _boards or probes < MAX_PROBES):
            probes += probe not in _boards
            board = _probe(probe)
            if board:
                x = _prefer_ats(x, board)
        if x.key in seen:
            continue
        seen.add(x.key)
        out.append(x)
    return out


def describe(listing: Listing) -> str:
    """The posting's text when the listing's link is a Greenhouse/Lever/Ashby posting ("" otherwise)."""
    ref = _ats_ref(listing.url)
    if not ref or not ref.job_id:
        return ""
    return SINGLE[ref.ats](ref.slug, ref.job_id).description[:MAX_DESC]


FEED = register_list(
    ListFeed(
        name="hn_jobstories",
        fetch=fetch,
        describe=describe,
        default=True,
        description="Hacker News job stories (news.ycombinator.com/jobs): the 'X (YC S19) Is Hiring' posts",
        terms="Official HN Firebase API (v0), public, no key, no stated rate limit; appa stays under 1 request/second "
        "and caches items. Posts belong to their authors; Work at a Startup / ycombinator.com links need a YC "
        "account to apply (marked ats=workatastartup, apply by hand). Probes public Greenhouse/Lever/Ashby APIs.",
    )
)
