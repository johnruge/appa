"""BambooHR careers sites (https://{slug}.bamboohr.com/careers), through the JSON the public careers page uses.

`/careers/list` returns every open posting in one response (no pagination, no dates, no text);
`/careers/company-info` gives the company's display name; `/careers/{id}/detail` has the description,
pay and posting date, so describe() fetches it only for postings that pass appa's free filters. No login
or key; robots.txt only disallows the old /jobs/embed pages. Old `/jobs/view.php?id=N` links redirect to
`/careers/N`, so both are recognised as boards.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlparse

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")  # one DNS label under bamboohr.com
_ID = re.compile(r"^[0-9]{1,12}$")
_NOT_BOARDS = {
    "www",
    "api",
    "app",
    "help",
    "partners",
    "marketplace",
    "status",
    "go",
    "get",
    "staticfe",
    "images",
    "images7",
    "resources",
    "community",
    "developers",
    "documentation",
    "login",
    "my",
}
_POSTING = re.compile(r"^/careers/([0-9]{1,12})(?:/|$)")
_EMPLOYMENT = {
    "full-time": "full_time",
    "part-time": "part_time",
    "intern": "internship",
    "internship": "internship",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "seasonal": "temporary",
}
_REMOTE = {"1": "yes", "2": "hybrid"}  # locationType: 0 on-site, 1 remote, 2 hybrid
MAX_DESCRIPTION = 100_000


def _slug(slug: str) -> str:
    """The validated, lowercased board slug; ValueError for anything that isn't one plain subdomain label."""
    if not isinstance(slug, str) or not _SLUG.match(slug) or slug.lower() in _NOT_BOARDS:
        raise ValueError(f"not a BambooHR board slug: {slug!r}")
    return slug.lower()


def _base(slug: str) -> str:
    """The careers root for a validated slug."""
    return f"https://{quote(_slug(slug), safe='')}.bamboohr.com/careers"


def _company(slug: str) -> str:
    """The company's display name from the careers site, falling back to the slug."""
    try:
        name = ((get_json(f"{_base(slug)}/company-info") or {}).get("result") or {}).get("name")
    except ValueError:  # unparseable body: the slug still names the board
        name = None
    return name.strip() if isinstance(name, str) and name.strip() else slug


def _location(row: dict) -> str:
    """'City, State' from the row's location (or its ATS location), skipping blanks and repeats."""
    loc = row.get("location") or {}
    ats = row.get("atsLocation") or {}
    parts = [
        loc.get("city") or ats.get("city"),
        loc.get("state") or ats.get("state") or ats.get("province"),
        ats.get("country"),
    ]
    out: list[str] = []
    for p in parts:
        if isinstance(p, str) and p.strip() and p.strip() not in out:
            out.append(p.strip())
    return ", ".join(out)


def _hints(row: dict) -> dict[str, str]:
    """Facts the list row states: employment type, remote/hybrid, department, country."""
    hints: dict[str, str] = {}
    label = str(row.get("employmentStatusLabel") or "").strip()
    if label:
        hints["employment_type"] = _EMPLOYMENT.get(label.lower(), label.lower().replace(" ", "_"))
        if label.lower() in ("intern", "internship"):
            hints["level"] = "intern"
    if row.get("isRemote") is True:
        hints["remote"] = "yes"
    elif str(row.get("locationType")) in _REMOTE:
        hints["remote"] = _REMOTE[str(row.get("locationType"))]
    if row.get("departmentLabel"):
        hints["category"] = str(row["departmentLabel"])
    country = (row.get("atsLocation") or {}).get("country")
    if isinstance(country, str) and country.strip():
        hints["country"] = country.strip()
    return hints


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's BambooHR careers site (no descriptions; see describe())."""
    opts = opts or FeedOptions()
    slug = _slug(slug)
    data = get_json(f"{_base(slug)}/list")
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return []
    company = _company(slug)
    out: list[Listing] = []
    seen: set[str] = set()
    for row in rows:
        if len(out) >= opts.max_results:
            break
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "")
        if not _ID.match(job_id) or job_id in seen:
            continue
        try:
            out.append(
                Listing(
                    company=company,
                    title=str(row.get("jobOpeningName") or ""),
                    url=f"https://{slug}.bamboohr.com/careers/{job_id}",
                    location=_location(row),
                    ats="bamboohr",
                    ats_job_id=job_id,
                    source_ref=f"{slug}:{job_id}",
                    hints=_hints(row),
                )
            )
            seen.add(job_id)
        except ValueError:
            continue
    return out


def _posting(url: str) -> tuple[str, str] | None:
    """(slug, posting id) of a BambooHR posting link, if it is one."""
    slug = board_of(url)
    if not slug:
        return None
    p = urlparse(url)
    m = _POSTING.match(p.path)
    if m:
        return slug, m.group(1)
    if p.path.rstrip("/") == "/jobs/view.php":
        job_id = (parse_qs(p.query).get("id") or [""])[0]
        if _ID.match(job_id):
            return slug, job_id
    return None


def describe(listing: Listing) -> str:
    """The posting's full text (with its posted pay, when given), from the careers detail endpoint."""
    ref = _posting(listing.url)
    if not ref:
        return ""
    slug, job_id = ref
    data = get_json(f"{_base(slug)}/{quote(job_id, safe='')}/detail")
    job = ((data or {}).get("result") or {}).get("jobOpening") if isinstance(data, dict) else None
    if not isinstance(job, dict):
        return ""
    text, hidden = html_to_text(str(job.get("description") or ""))
    text = mark_hidden(text, hidden)
    pay = job.get("compensation")
    if isinstance(pay, str) and pay.strip():
        text = f"Compensation: {pay.strip()}\n\n{text}"
    return text[:MAX_DESCRIPTION]


def board_of(url: str) -> str | None:
    """The BambooHR board slug in a careers/jobs link ({slug}.bamboohr.com/careers... or /jobs...), else None."""
    try:
        p = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if not host.endswith(".bamboohr.com"):
        return None
    label = host[: -len(".bamboohr.com")]
    if "." in label or not _SLUG.match(label) or label in _NOT_BOARDS:
        return None
    path = p.path.lower()
    if not (path == "/careers" or path.startswith("/careers/") or path == "/jobs" or path.startswith("/jobs/")):
        return None
    return label


BOARD = register_board(
    BoardFeed(
        ats="bamboohr",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="kiln",
        terms="Public careers-site JSON (no login); robots.txt disallows only /jobs/embed*.php. One list request "
        "plus one company-info request per board; describe() is one request per posting.",
        hosts=("bamboohr.com",),
    )
)
