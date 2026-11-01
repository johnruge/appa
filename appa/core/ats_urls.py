"""Recognize Greenhouse / Lever / Ashby links and pull out the board slug and job ID. Pure parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass
class AtsRef:
    ats: str
    slug: str
    job_id: str | None = None


_PATTERNS = [
    (
        "greenhouse",
        re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)(?:/jobs/(\d+))?"),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)(?:/([0-9a-f-]{36}))?")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)(?:/([0-9a-f-]{36}))?")),
]


def parse_url(url: str) -> AtsRef | None:
    """Recognize a Greenhouse/Lever/Ashby URL (or gh_jid/ashby_jid query param) and return its board and job id."""
    for ats, pat in _PATTERNS:
        m = pat.search(url)
        if m:
            job_id = m.group(2).lower() if m.group(2) else None
            return AtsRef(ats, m.group(1), job_id)
    # Company careers pages that embed an ATS carry its job id in the query string
    qs = parse_qs(urlparse(url).query)
    if "gh_jid" in qs:
        return AtsRef("greenhouse", "", qs["gh_jid"][0])
    if "ashby_jid" in qs:
        return AtsRef("ashby", "", qs["ashby_jid"][0].lower())
    return None
