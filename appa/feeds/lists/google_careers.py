"""Google Careers: every open role at Google, YouTube and DeepMind, from the public XML job feed Google
publishes for job aggregators (google.com/about/careers/applications/jobs/feed.xml, ~3,500 jobs, ~20 MB,
refreshed about every 25 minutes). One request returns everything, full descriptions included, so
there's no search or paging. robots.txt allows the feed (it only disallows paging the HTML results).
"""

from __future__ import annotations

import re
from datetime import date, datetime

import defusedxml.ElementTree as ET

from appa.feeds import FeedOptions, ListFeed, Listing, register_list
from appa.sources.http import get_bytes
from appa.sources.text import html_to_text, mark_hidden

URL = "https://www.google.com/about/careers/applications/jobs/feed.xml"
JOB_PAGE = "https://www.google.com/about/careers/applications/jobs/results/"
MAX_DESC = 100_000
_ID = re.compile(r"^\d{1,30}$")
_SLUG = re.compile(r"^[A-Za-z0-9-]{0,200}$")
_SALARY = re.compile(r"\$[\d,]+(?:\.\d+)?\s*[-–]\s*\$[\d,]+(?:\.\d+)?")
_US = ("USA", "US", "United States", "United States of America")
_CITY_STATES = {"singapore": "Singapore", "hong kong": "Hong Kong", "macau": "Macau", "macao": "Macau"}


def _text(el, path: str) -> str:
    """Stripped text of a child element ('' when missing)."""
    return (el.findtext(path) or "").strip()


def _date(s: str) -> date | None:
    """An ISO timestamp from the feed as a date (None if missing or invalid)."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _url(job_id: str, feed_url: str) -> str:
    """The posting page on google.com, rebuilt from the validated ID and the feed link's title slug."""
    last = feed_url.rstrip("/").rsplit("/", 1)[-1]
    slug = last[len(job_id) + 1 :] if last.startswith(job_id + "-") else ""
    slug = slug if _SLUG.match(slug) else ""
    return f"{JOB_PAGE}{job_id}-{slug}/" if slug else f"{JOB_PAGE}{job_id}/"


def _country(loc) -> str:
    """The location's country: 'CA, USA' -> 'USA', bare 'CA' -> 'Canada', empty -> the city if it's a city-state."""
    raw = _text(loc, "country")
    if not raw:
        return _CITY_STATES.get(_text(loc, "city").lower(), "") if not _text(loc, "state") else ""
    country = raw.rsplit(",", 1)[-1].strip()
    return "Canada" if raw == "CA" else country


def _level(title: str, jobtype: str) -> str:
    """Seniority as the title or job type states it ('' when it doesn't)."""
    t = title.lower()
    if jobtype == "INTERN":
        return "intern"
    if "university graduate" in t:
        return "new_grad"
    if "early career" in t:
        return "entry"
    return ""


def _listing(job) -> Listing | None:
    """A Listing from one <job> element, or None if it's malformed."""
    job_id, title = _text(job, "jobid"), _text(job, "title")
    if not _ID.match(job_id) or not title:
        return None
    locs = job.findall("locations/location")
    places = [", ".join(p for p in (_text(x, "city"), _text(x, "state"), _text(x, "country")) if p) for x in locs]
    countries = list(dict.fromkeys(c for c in map(_country, locs) if c))
    text, hidden = html_to_text(_text(job, "description"))
    jobtype = _text(job, "jobtype")
    salary = _SALARY.search(text)
    hints = {
        "category": ", ".join(c.text.strip() for c in job.findall("categories/category") if c.text),
        "country": ", ".join(countries),
        # isRemote wins over isHybrid: every live isHybrid=Yes row also has isRemote=Yes.
        "remote": "yes" if _text(job, "isRemote") == "Yes" else "hybrid" if _text(job, "isHybrid") == "Yes" else "no",
        "employment_type": jobtype.lower(),
        "level": _level(title, jobtype),
        "salary": salary.group(0) if salary else "",
    }
    try:
        return Listing(
            company=_text(job, "employer") or "Google",
            title=title,
            url=_url(job_id, _text(job, "url")),
            location="; ".join(p for p in places if p)[:500],
            posted_at=_date(_text(job, "published")),
            description=mark_hidden(text, hidden)[:MAX_DESC],
            ats="google",
            ats_job_id=job_id,
            source_ref=job_id,
            hints=hints,
        )
    except ValueError:
        return None


def _is_us(x: Listing) -> bool:
    """True when a location is in the US, or the feed gives no country at all (don't drop the unknown)."""
    countries = [c.strip() for c in x.hints.get("country", "").split(",") if c.strip()]
    return not countries or any(c in _US for c in countries)


def fetch(opts: FeedOptions) -> list[Listing]:
    """Every open Google/YouTube/DeepMind role (newest first), filtered by opts.since and opts.us_only."""
    root = ET.fromstring(get_bytes(URL))
    if root.tag != "jobs":
        raise ValueError("Google careers feed is not a <jobs> document")
    out = [x for x in map(_listing, root.iter("job")) if x is not None]
    if opts.since:
        out = [x for x in out if x.posted_at is None or x.posted_at >= opts.since]
    if opts.us_only:
        out = [x for x in out if _is_us(x)]
    out.sort(key=lambda x: x.posted_at or date.min, reverse=True)
    return out[: opts.max_results]


FEED = register_list(
    ListFeed(
        name="google_careers",
        description="Google Careers XML feed: every open Google, YouTube and DeepMind role, full descriptions",
        fetch=fetch,
        terms="Public feed Google publishes for job aggregators; robots.txt allows it. One ~20 MB request per run; "
        "it's cached 25 minutes, so don't poll more often. Personal use; Google's Terms of Service apply.",
    )
)
