"""Job IDs from apply/posting links. The job ID is what makes a posting unique:
the same title at the same company can be several postings (different
locations, teams, or cycles), each with its own ID.

Returns a key like "greenhouse:4000123" or "apply.careers.microsoft.com:1970000000000001".
Greenhouse/Lever/Ashby IDs are global, so they're namespaced by ATS; other IDs are
only unique per site, so they're namespaced by hostname.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from appa.core.ats_urls import parse_url

# Query parameters that carry a job ID on various career sites.
_ID_PARAMS = ("pid", "jobid", "job_id", "jid", "jobseqno", "reqid", "requisitionid", "req")

_HOST_PATTERNS = [
    (re.compile(r"linkedin\.com$"), re.compile(r"/jobs/view/(\d+)")),
    # Workday: .../job/<Location>/<Title>_<ReqId>   e.g. Software-Engineer-I_JR-259943
    (re.compile(r"myworkdayjobs\.com$"), re.compile(r"/job/(?:[^/]+/)*[^/]*_([A-Za-z0-9-]*\d[A-Za-z0-9-]*)/?$")),
    (re.compile(r"icims\.com$"), re.compile(r"/jobs/(\d+)")),
    (re.compile(r"breezy\.hr$"), re.compile(r"/p/([0-9a-f]{8,})")),
]

# Path segments that are never IDs, skipped when scanning a path from the end.
_SKIP = {"apply", "application", "thanks", "success", "job", "jobs", "details", "view", "login"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX24 = re.compile(r"^[0-9a-f]{24}$")
_NUM = re.compile(r"^\d{4,20}$")
_LEADING_NUM = re.compile(r"^(\d{5,})-")


def _looks_like_year(s: str) -> bool:
    """True for a 4-digit string between 1990 and 2100."""
    return len(s) == 4 and 1990 <= int(s) <= 2100


def job_key(url: str | None) -> str | None:
    """A stable job identity from a posting URL (ATS id, id query param, or an id-looking path segment), or None."""
    if not url:
        return None
    url = url.strip()
    ref = parse_url(url)
    if ref and ref.job_id:
        return f"{ref.ats}:{ref.job_id}"

    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    if not host:
        return None

    qs = {k.lower(): v for k, v in parse_qs(p.query).items()}
    for name in _ID_PARAMS:
        if name in qs and qs[name][0].strip():
            return f"{host}:{qs[name][0].strip().lower()}"

    for host_pat, path_pat in _HOST_PATTERNS:
        if host_pat.search(host):
            m = path_pat.search(p.path)
            if m:
                return f"{host}:{m.group(1).lower()}"
            return None  # known site, but this link (e.g. a confirmation page) has no ID

    for seg in reversed([s for s in p.path.lower().split("/") if s]):
        if seg in _SKIP:
            continue
        if _UUID.match(seg) or _HEX24.match(seg):
            return f"{host}:{seg}"
        if _NUM.match(seg) and not _looks_like_year(seg):
            return f"{host}:{seg}"
        m = _LEADING_NUM.match(seg)
        if m:
            return f"{host}:{m.group(1)}"
    return None


# ---- IDs that don't come from a known link pattern -------------------------------------
# From the page's structured data, an AI read of the page, you / your agent, or your notes.
# They're scoped to the site's host (IDs are only unique per site), or to the company when
# there's no link at all.

_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{1,99}$")


def clean_id(raw: str | None) -> str | None:
    """Normalize a raw job ID to lowercase; None if it is malformed, has no digits, or is just a year."""
    v = (raw or "").strip().strip("#").strip()
    if not _ID_OK.match(v) or not re.search(r"\d", v):  # job IDs carry digits; "engineer" is not an ID
        return None
    if re.fullmatch(r"(19|20)\d\d", v):  # a year, not an ID
        return None
    return v.lower()


def compose(scope: str, job_id: str | None) -> str | None:
    """Join a scope and a cleaned job ID as 'scope:id', or None if either is missing."""
    jid = clean_id(job_id)
    return f"{scope}:{jid}" if jid and scope else None


def url_key(url: str) -> str:
    """A link as an identity when there's no job ID: the link itself, or a hash of it when it's too long to store."""
    import hashlib

    url = url.strip()
    return url if len(url) <= 480 else "url:" + hashlib.sha256(url.encode()).hexdigest()


def resolve_key(link: str | None, job_id: str | None = None, company: str | None = None) -> str | None:
    """The posting's identity: a recognized link pattern wins; otherwise a known job ID,
    scoped to the link's host, or to the company when there's no link."""
    key = job_key(link)
    if key or not job_id:
        return key
    host = (urlparse((link or "").strip()).hostname or "").lower().removeprefix("www.")
    if host:
        return compose(host, job_id)
    from appa.core.normalize import company as norm

    c = norm(company)
    return compose(f"co:{c}", job_id) if c else None


def verified(job_id: str | None, *haystacks: str | None) -> bool:
    """An ID read by a model only counts if it appears word for word in the link or page."""
    jid = clean_id(job_id)
    if not jid:
        return False
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(jid)}(?![A-Za-z0-9])", re.I)
    return any(h and pat.search(h) for h in haystacks)


_NOTE_PATTERNS = [
    re.compile(
        r"\b(?:job\s*id|job\s*#|req(?:uisition)?(?:\s*id)?\s*#?|pid|posting)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{3,40})",
        re.I,
    ),
    re.compile(r"\b(JR-?\d{4,})\b", re.I),
    re.compile(r"\((\d{6,})\)"),  # e.g. "amazon (10567489)"
]


def ids_in_text(*texts: str | None) -> list[str]:
    """Job IDs written in your own tracker notes, company or role text."""
    found: list[str] = []
    for t in texts:
        for pat in _NOTE_PATTERNS:
            for m in pat.finditer(t or ""):
                jid = clean_id(m.group(1))
                if jid and jid not in found:
                    found.append(jid)
    return found
