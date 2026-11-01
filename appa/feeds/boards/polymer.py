"""Polymer job boards (jobs.polymer.co/{slug}), through the public JSON API its job board pages use (no key).

GET https://api.polymer.co/v1/hire/organizations/{slug}/jobs?page=N returns {items[], meta{page, next_page,
total, is_last, count, organization_name}}. Each item has the title, location, remoteness, kind (Full-time...),
category, salary text, published_at and the human posting link (job_post_url, jobs.polymer.co/{slug}/{id}),
but no description. The per-job endpoint .../jobs/{id} adds the description HTML, so describe() fetches it
only for postings that pass appa's free filters. Pagination is quirky: past the last page the API still
reports a next_page, so paging stops on an empty page too. Unknown or closed orgs answer 422, which fetch turns
into a short RuntimeError. Slugs are case-sensitive. Some orgs publish job_post_url on their own careers domain
(e.g. jobs.violetlabs.com/40985); those links are kept as the posting URL, describe() finds the org through
source_ref ("{slug}:{id}"), and board_of only recognises jobs.polymer.co links, so such boards are found only
through jobs.polymer.co/{slug} links.
Polymer's terms only forbid automated tools that could impair the service; robots.txt allows all.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from urllib.parse import quote, urlparse

import httpx

from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources.http import get_json
from appa.sources.text import html_to_text, mark_hidden

log = logging.getLogger(__name__)

API = "https://api.polymer.co/v1/hire/organizations"
HOST = "jobs.polymer.co"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ID = re.compile(r"^[0-9]{1,20}$")
_NOT_BOARDS = {"robots.txt", "favicon.ico", "sitemap.xml", "api", "assets", "static", "login", "signup"}
_TYPES = {
    "full-time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "internship": "internship",
    "freelance": "contract",
    "temporary": "temporary",
}
MAX_PAGES = 50
PAUSE = 0.5
MAX_DESC = 100_000


def _check(slug: str) -> str:
    """The slug, if it is a safe Polymer organization slug; else ValueError."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.lower() in _NOT_BOARDS:
        raise ValueError(f"bad polymer slug: {slug!r}")
    return slug


def jobs_url(slug: str) -> str:
    """The API URL listing a board's jobs."""
    return f"{API}/{quote(_check(slug), safe='')}/jobs"


def _day(v) -> date | None:
    """'2026-07-02T16:04:55.201Z' -> date, or None."""
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _remote(v: str) -> str:
    """'Remote' / 'Hybrid' / 'On-site' -> the remote hint value."""
    v = v.lower()
    return "hybrid" if "hybrid" in v else "yes" if "remote" in v else "no" if v else ""


def _hints(j: dict) -> dict[str, str]:
    """The facts the job states, under HINT_KEYS names."""
    countries = [str(c) for c in (j.get("remote_restriction_country_list") or []) if c]
    if j.get("country") and str(j["country"]) not in countries:
        countries.insert(0, str(j["country"]))
    kind = str(j.get("kind_pretty") or "")
    return {
        "remote": _remote(str(j.get("remoteness_pretty") or "")),
        "country": ", ".join(countries),
        "employment_type": _TYPES.get(kind.lower(), kind),
        "category": str(j.get("job_category_name") or ""),
        "salary": str(j.get("salary_pretty") or ""),
    }


def _location(j: dict) -> str:
    """The display location, marked Remote when the job is remote."""
    loc = str(
        j.get("display_location") or ", ".join(str(j[k]) for k in ("city", "state_region", "country") if j.get(k))
    )
    rem = str(j.get("remoteness_pretty") or "")
    if _remote(rem) in ("yes", "hybrid") and rem.lower() not in loc.lower():
        loc = f"{rem} ({loc})" if loc else rem
    return loc


def _listing(j: dict, slug: str, company: str) -> Listing:
    """One job as a Listing (ValueError when a required field is missing or bad)."""
    if not isinstance(j, dict):
        raise ValueError("not a job")
    jid = str(j.get("id") or j.get("job_id") or "")
    if not _ID.fullmatch(jid):
        raise ValueError("job has no numeric id")
    url = str(j.get("job_post_url") or f"https://{HOST}/{quote(slug, safe='')}/{jid}")
    return Listing(
        company=str(j.get("organization_name") or company or slug),
        title=str(j.get("title") or ""),
        url=url,
        location=_location(j),
        posted_at=_day(j.get("published_at") or j.get("created_at")),
        ats="polymer",
        ats_job_id=jid,
        source_ref=f"{slug}:{jid}",
        hints=_hints(j),
    )


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open job on this company's Polymer board (no descriptions; see describe)."""
    opts = opts or FeedOptions()
    url = jobs_url(slug)
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        try:
            data = get_json(url, params={"page": page})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                raise RuntimeError(f"polymer org not found or has no careers page: {slug}") from None
            raise
        items = (data.get("items") if isinstance(data, dict) else None) or []
        meta = (data.get("meta") if isinstance(data, dict) else None) or {}
        for j in items:
            try:
                x = _listing(j, slug, str(meta.get("organization_name") or ""))
            except ValueError as e:
                log.debug("skipping polymer job on %s: %s", slug, e)
                continue
            if x.ats_job_id in seen or (opts.since and x.posted_at and x.posted_at < opts.since):
                continue
            seen.add(x.ats_job_id)
            out.append(x)
            if len(out) >= opts.max_results:
                return out
        total = meta.get("total")
        if not items or not meta.get("next_page") or meta.get("is_last") or (isinstance(total, int) and page >= total):
            break
    return out


def _parts(url: str) -> tuple[str, str | None] | None:
    """(slug, job id or None) from a jobs.polymer.co link, or None."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        return None
    if p.scheme not in ("http", "https") or host != HOST:
        return None
    segs = [s for s in p.path.split("/") if s]
    if not segs or (len(segs) > 1 and not _ID.fullmatch(segs[1])):
        return None
    try:
        return _check(segs[0]), segs[1] if len(segs) > 1 else None
    except ValueError:
        return None


def board_of(url: str) -> str | None:
    """The board slug of a jobs.polymer.co/{slug}[/{id}] link, or None."""
    got = _parts(url)
    return got[0] if got else None


def _ref(ref: str) -> tuple[str, str] | None:
    """(slug, job id) from a "{slug}:{id}" source_ref, or None when it doesn't parse."""
    slug, sep, jid = str(ref or "").rpartition(":")
    if not sep or not _ID.fullmatch(jid):
        return None
    try:
        return _check(slug), jid
    except ValueError:
        return None


def describe(listing: Listing) -> str:
    """The posting's full text from the per-job endpoint (one request), hidden text flagged."""
    got = _ref(listing.source_ref)
    if not got:
        parts = _parts(listing.url)
        jid = parts[1] if parts and parts[1] else listing.ats_job_id
        if not parts or not jid or not _ID.fullmatch(str(jid)):
            return ""
        got = (parts[0], str(jid))
    data = get_json(f"{jobs_url(got[0])}/{quote(got[1], safe='')}")
    text, hidden = html_to_text(str((data or {}).get("description") or "") if isinstance(data, dict) else "")
    return mark_hidden(text, hidden)[:MAX_DESC]


BOARD = register_board(
    BoardFeed(
        ats="polymer",
        fetch=fetch,
        board_of=board_of,
        describe=describe,
        example="themis",
        terms="Public job board API (no key) behind jobs.polymer.co; terms only bar tools that impair the service. "
        "Paged at ~0.5s between pages; descriptions fetched per job.",
        hosts=("api.polymer.co",),
    )
)
