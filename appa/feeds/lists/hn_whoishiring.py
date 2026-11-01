"""Hacker News "Ask HN: Who is hiring?": the monthly thread (posted by the whoishiring account) where
hiring companies leave one top-level comment each. appa reads it through the free Algolia HN API
(https://hn.algolia.com/api, no key, about 10,000 requests/hour per IP): one search finds the latest
threads, one items request per thread returns the whole comment tree.

Each top-level comment becomes one Listing. Most start with a line like
"Company | Role(s) | Location | REMOTE/ONSITE | Full-time | $salary"; that line is parsed defensively
(" — " dashes also separate parts; a comment with neither is kept only if it links a job/ATS page, so
meta chatter stays out). The link is the first job/ATS page in the comment, else the comment itself;
other links go to hints["links"] so appa discovers the company boards. When the link carries no job ID
(a careers page, the comment), the comment ID is the posting ID, so each monthly post is its own job.
When the first line names no role, the title comes from the link's slug or the first role in the text.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from appa.core.jobid import job_key
from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/"
HN_ITEM = "https://news.ycombinator.com/item?id="
TITLE_PREFIX = "Ask HN: Who is hiring?"
MAX_THREADS = 2  # the latest month, plus the previous one when opts.since allows
PAUSE = 0.5

ATS_HOSTS = (
    "greenhouse.io",
    "grnh.se",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "recruitee.com",
    "bamboohr.com",
    "breezy.hr",
    "jobvite.com",
    "icims.com",
    "wellfound.com",
    "teamtailor.com",
    "personio.de",
    "personio.com",
    "rippling.com",
    "gem.com",
    "workatastartup.com",
    "jazzhr.com",
    "applytojob.com",
    "pinpointhq.com",
    "dover.com",
    "homerun.co",
    "join.com",
    "polymer.co",
    "notion.site",
)
JOB_PATH = re.compile(
    r"/(jobs?|careers?|positions?|openings?|apply|vacanc\w*|join[-_]?us|hiring|work-with-us)\b"
    r"|[?&](gh_jid|ashby_jid|lever-source|jobid|job_id)=",
    re.I,
)
SKIP_HOSTS = ("news.ycombinator.com", "linkedin.com")
HREF = re.compile(r'href="([^"]+)"', re.I)

ROLE = re.compile(
    r"engineer|developer|scientist|designer|manager|\bswe\b|\bsre\b|devops|architect|analyst"
    r"|\blead\b|research|programmer|\bhead of\b|founding|\bcto\b|\bvp\b|\broles?\b|positions"
    r"|\bml\b|\bai\b|marketing|sales|recruiter|operations|product|full[- ]?stack|backend|frontend"
    r"|systems|infrastructure|\binfra\b|\bops\b",
    re.I,
)
MODE = re.compile(r"\b(remote|onsite|on-site|hybrid|in[- ]office)\b", re.I)
PLACE = re.compile(
    r",|\b(us|usa|uk|eu|emea|apac|canada|europe|worldwide|global|nyc|sf|bay area|new york"
    r"|san francisco|london|berlin|paris|amsterdam|toronto|seattle|boston|austin|chicago"
    r"|los angeles|denver|remote|onsite|hybrid|anywhere|timezones?)\b",
    re.I,
)
SALARY = re.compile(r"[$€£]\s?\d|\d\s?k\b|\b(usd|eur|gbp|cad)\b", re.I)
EMPLOYMENT = {
    "full_time": re.compile(r"full[- ]?time|\bperm", re.I),
    "part_time": re.compile(r"part[- ]?time", re.I),
    "contract": re.compile(r"contract|freelance", re.I),
    "internship": re.compile(r"\bintern(ship)?s?\b", re.I),
}
VISA = re.compile(r"visa|sponsor", re.I)
LEVEL_NEW_GRAD = re.compile(r"new[- ]?grads?|early[- ]career|(?<!post)(?<!under)graduate (program|role|engineer)", re.I)
ROLE_NOUN = (
    r"(?:engineer|developer|scientist|designer|swe|sre|architect|analyst|programmer|manager"
    r"|researcher|intern|recruiter)s?"
)
ROLE_PHRASE = re.compile(
    r"\b((?:(?:[A-Z][\w+#/.&-]*|of|and|&)\s+){0,5}(?:Engineer|Developer|Scientist|Designer"
    r"|SWE|SRE|Architect|Analyst|Programmer|Manager|Researcher|Intern|Recruiter)s?)\b"
)
ROLE_WORD = re.compile(r"\b" + ROLE_NOUN + r"\b", re.I)
SWE_TEXT = re.compile(r"software (engineer|developer)|\bswe\b|full[- ]?stack|backend|frontend", re.I)
DOMAIN = re.compile(r"[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?", re.I)
DASH = re.compile(r"\s+[—–]\s+")
CITIZEN = re.compile(r"u\.?s\.? citizen|citizenship|security clearance|clearance required|\bts/sci\b", re.I)


def _date(ts) -> date | None:
    """A unix timestamp as a UTC date (None if missing or invalid)."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _threads(opts: FeedOptions) -> list[dict]:
    """The newest "Who is hiring?" stories to read: the latest, plus the previous one unless opts.since
    is on or after the latest thread's date (newest first)."""
    data = get_json(SEARCH_URL, params={"tags": "story,author_whoishiring", "hitsPerPage": 20})
    hits = [
        h
        for h in (data.get("hits") or [])
        if isinstance(h, dict)
        and str(h.get("title") or "").startswith(TITLE_PREFIX)
        and str(h.get("objectID") or "").isdigit()
    ]
    hits.sort(key=lambda h: int(h.get("created_at_i") or 0), reverse=True)
    if not hits:
        return []
    latest = _date(hits[0].get("created_at_i"))
    if opts.since and latest and opts.since >= latest:
        return hits[:1]
    return hits[:MAX_THREADS]


def _host(url: str) -> str:
    """Lower-case host of a link ('' if it has none)."""
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _under(host: str, domains: tuple[str, ...]) -> bool:
    """True if host is one of the domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _links(raw: str) -> list[str]:
    """The comment's http(s) links in order, without duplicates or HN/LinkedIn links."""
    out: list[str] = []
    for href in HREF.findall(raw):
        url = html.unescape(href).strip()
        if (
            url.lower().startswith(("http://", "https://"))
            and len(url) <= 2000
            and url not in out
            and _host(url)
            and not _under(_host(url), SKIP_HOSTS)
        ):
            out.append(url)
    return out


def _is_job_link(url: str) -> bool:
    """True if a link looks like a job posting, careers page or ATS board."""
    p = urlparse(url)
    return _under(_host(url), ATS_HOSTS) or bool(JOB_PATH.search(p.path + ("?" + p.query if p.query else "")))


def _segments(first_line_html: str) -> list[str]:
    """The parts of the first line split on pipes (else on spaced dashes), as plain text (empty parts kept,
    so a missing company shows)."""
    text, _ = html_to_text(first_line_html)
    line = " ".join(text.split("\n"))
    parts = line.split("|") if "|" in line else DASH.split(line)
    return [" ".join(x.split()) for x in parts]


def _is_domain(seg: str) -> bool:
    """True if a segment is just a bare domain or link (e.g. 'reef.pl', 'https://x.com/')."""
    bare = re.sub(r"^https?://", "", seg.strip(), flags=re.I)
    return bool(bare) and bool(DOMAIN.fullmatch(bare))


def _company(seg: str) -> str:
    """The company name from the first segment, without links or a trailing '(YC S24)'-style note."""
    name = re.sub(r"https?://\S+", "", seg)
    name = re.sub(r"\([^)]*\)", "", name).strip(" -–—:,()")
    if not name and _is_domain(seg):
        name = _host(seg if "://" in seg else "https://" + seg).removeprefix("www.")
    return " ".join(name.split()) or seg


def _loose_company(line: str) -> str:
    """The company from a pipe-less first line: the text before the first '(', ' - ' or ',' (a domain loses
    its path)."""
    name = re.split(r"\(|\s-\s|,|:", line, maxsplit=1)[0].strip()
    if _is_domain(name):
        name = name.split("/")[0] if "://" not in name else _host(name)
    return " ".join(name.split()[:6])


def _parse_head(segs: list[str]) -> tuple[str, str, dict[str, str]]:
    """(title, location, hints) from the first line's segments after the company ('' title if none)."""
    title, places, hints = "", [], {}
    for seg in segs[1:]:
        bare = re.sub(r"https?://\S+", "", seg).strip()
        if not bare or re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", bare, re.I):
            continue  # just a link or a domain
        visa, pay = VISA.search(bare), SALARY.search(bare)
        if visa:
            hints.setdefault("sponsorship", bare[:300])
        if pay:
            hints.setdefault("salary", bare[:300])
        if visa or pay:
            continue
        if len(bare) < 40 and any(rx.search(bare) for rx in EMPLOYMENT.values()) and not ROLE.search(bare):
            kinds = [k for k, rx in EMPLOYMENT.items() if rx.search(bare)]
            hints.setdefault("employment_type", ", ".join(kinds))
        elif not title and ROLE.search(bare) and not MODE.fullmatch(bare):
            title = bare
        elif PLACE.search(bare) or MODE.search(bare):
            places.append(bare)
    line = " ".join(segs).lower()
    remote, onsite, hybrid = (
        bool(re.search(r"\bremote", line)),
        bool(re.search(r"\bon-?site\b|in[- ]office", line)),
        "hybrid" in line,
    )
    if hybrid:
        hints["remote"] = "hybrid"
    elif remote and not onsite:
        hints["remote"] = "yes"
    elif onsite and not remote:
        hints["remote"] = "no"
    if LEVEL_NEW_GRAD.search(line):
        hints["level"] = "new_grad"
    elif re.search(r"entry[- ]level|junior", line):
        hints["level"] = "entry"
    elif re.search(r"\bintern(ship)?s?\b", line) and not re.search(r"full[- ]?time", line):
        hints["level"] = "intern"
    if CITIZEN.search(line):
        hints["citizenship_required"] = "yes"
    return title, "; ".join(places[:2])[:500], hints


def _slug_title(url: str | None) -> str:
    """A role title from a job link's last path slug ('.../18180624-software-engineer' -> 'Software Engineer'),
    or '' if the slug names no role."""
    if not url:
        return ""
    slug = next((s for s in reversed(urlparse(url).path.split("/")) if s), "")
    words = [w for w in re.split(r"[-_+]+", re.sub(r"\.\w+$", "", slug)) if w and not w.isdigit()]
    text = " ".join(words)
    return text.title() if ROLE_WORD.search(text) and len(text) <= 120 and len(words) <= 12 else ""


def _body_title(text: str) -> str:
    """The first capitalised role phrase after the comment's first line (e.g. 'Staff Software Engineer'), or ''."""
    for line in text.split("\n")[1:40]:
        m = ROLE_PHRASE.search(line)
        if m:
            return re.sub(r"^(?:of|and|&)\s+", "", m.group(1)).strip()
    return ""


def _listing(c: dict) -> Listing | None:
    """A Listing from one top-level comment, or None for deleted/dead/unparseable ones."""
    raw, cid = c.get("text"), c.get("id")
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or not c.get("author")
        or c.get("dead")
        or c.get("deleted")
        or c.get("type", "comment") != "comment"
        or not str(cid or "").isdigit()
    ):
        return None
    head = re.split(r"<p>|\n", raw, maxsplit=1)[0]
    segs = _segments(head)
    links = _links(raw)
    url = next((u for u in links if _is_job_link(u)), None)
    if not segs[0]:
        return None
    if len(segs) < 2:
        if not url:  # no separators and no job link: chatter, not a post
            return None
        company = _loose_company(segs[0])
    else:
        if ROLE.search(segs[0]) and not _is_domain(segs[0]):  # 'Role | reef.pl | ...': company is the domain
            dom = next((i for i, s in enumerate(segs[1:], 1) if _is_domain(s)), None)
            if dom:
                segs[0], segs[dom] = segs[dom], segs[0]
        company = _company(segs[0])
    if not company:
        return None
    title, location, hints = _parse_head(segs)
    text, hidden = html_to_text(raw)
    if not title:
        title = (
            _slug_title(url)
            or _body_title(text)
            or ("Software Engineer (see post)" if SWE_TEXT.search(text) else "Hiring (see post)")
        )
    others: list[str] = []
    for u in (u for u in links if u != url):
        if len(others) == 5 or len(" ".join([*others, u])) > 300:  # hints are capped at 300 chars
            break
        others.append(u)
    if others:
        hints["links"] = " ".join(others)
    link = url or f"{HN_ITEM}{cid}"
    try:
        return Listing(
            company=company[:300],
            title=title[:500],
            url=link,
            location=location,
            posted_at=_date(c.get("created_at_i")),
            description=mark_hidden(text, hidden)[:100_000],
            ats_job_id=None if job_key(link) else f"hn{cid}",
            source_ref=f"hn{cid}",
            hints=hints,
        )
    except ValueError:
        return None


def fetch(opts: FeedOptions) -> list[Listing]:
    """One Listing per hiring comment in the latest thread(s), newest first, deduped by posting key."""
    out: list[Listing] = []
    seen: set[str] = set()
    for i, story in enumerate(_threads(opts)):
        if i:
            time.sleep(PAUSE)
        tree = get_json(ITEM_URL + str(int(story["objectID"])))
        for c in tree.get("children") or []:
            x = _listing(c) if isinstance(c, dict) else None
            if x is None or x.key in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
                continue
            seen.add(x.key)
            out.append(x)
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="hn_whoishiring",
        description="Hacker News 'Ask HN: Who is hiring?' monthly thread (via Algolia HN API), one listing per "
        "company comment",
        fetch=fetch,
        terms="Free public Algolia HN API, no key; about 10,000 requests/hour per IP. Comments belong to their "
        "authors; "
        "personal use only. Many posts ask for email applications or say no recruiters/automated applications.",
    )
)
