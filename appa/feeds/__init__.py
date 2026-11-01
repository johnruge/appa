"""Feeds: every place appa can get job postings from, without a login or a model call.

Two kinds of source:

- **Lists** (`appa/feeds/lists/*.py`) return many companies' postings at once: curated new-grad lists,
  job APIs, remote boards. Register one with `register_list(ListFeed(...))`.
- **Boards** (`appa/feeds/boards/*.py`) return one company's postings from its ATS (Greenhouse,
  Workday, SmartRecruiters...). Register one with `register_board(BoardFeed(...))`. Boards are found
  automatically: every link any source yields is checked against each board's `board_of()`, and new
  boards are stored and polled from then on (see appa/feeds/store.py).

Modules in those two folders are discovered at runtime, so adding a source never edits a shared file.
Every request goes through appa/sources/http.py (public hosts only, size caps). Feed text is untrusted:
it's data for the screener, never instructions.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field, field_validator

from appa.core.jobid import resolve_key, url_key
from appa.core.models import Job

log = logging.getLogger(__name__)

KNOWN_ATS = ("greenhouse", "lever", "ashby")  # the ATSs the pipeline and apply flow have tuned support for

# Facts a source states about a posting. Use these keys when the source has the fact; values are short text.
HINT_KEYS = {
    "sponsorship": "the source's own sponsorship label, verbatim (e.g. 'Does Not Offer Sponsorship')",
    "citizenship_required": "'yes' when the source says U.S. citizenship (or clearance) is required",
    "level": "seniority the source states: 'entry', 'new_grad', 'mid', 'senior', 'intern'",
    "degrees": "degrees the source lists, comma separated",
    "category": "the source's category for the role (e.g. 'Software', 'Data Science')",
    "remote": "'yes' (fully remote), 'hybrid' or 'no'",
    "country": "country names or codes the source gives, comma separated",
    "salary": "the posted pay range, as the source shows it",
    "employment_type": "full_time, part_time, contract, internship...",
    "links": "other job/ATS links in the posting, space separated (used to discover company boards)",
}


class Listing(BaseModel):
    """One posting as a source lists it. Becomes a Job when it's screened."""

    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)  # the posting (or its apply page)
    location: str = Field("", max_length=500)
    posted_at: date | None = None
    description: str = Field("", max_length=200_000)  # full posting text, when the source has it
    ats: str = "unknown"  # the ATS it's hosted on, when the source knows
    ats_job_id: str | None = Field(None, max_length=100)  # the posting's ID on that ATS/site, if known
    source_ref: str | None = Field(None, max_length=300)  # the source's own id for the posting
    hints: dict[str, str] = Field(default_factory=dict)  # see HINT_KEYS

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        """Only http(s) links; anything else can't be fetched or applied to."""
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("listing url must be http(s)")
        return v

    @field_validator("company", "title", "location")
    @classmethod
    def _one_line(cls, v: str) -> str:
        """Collapse whitespace so names and titles compare cleanly."""
        return " ".join(v.split())

    @field_validator("hints")
    @classmethod
    def _small(cls, v: dict[str, str]) -> dict[str, str]:
        """Keep hints short and string-valued; drop empty ones."""
        return {str(k)[:40]: " ".join(str(x).split())[:300] for k, x in list(v.items())[:20] if x not in (None, "")}

    @property
    def key(self) -> str:
        """The same identity the pipeline uses: the job ID in the link, else the source's ID scoped to the
        link's host, else the link."""
        return resolve_key(self.url, self.ats_job_id, self.company) or url_key(self.url)

    def to_job(self, source: str) -> Job:
        """The Job the screener reads (description may be empty: the scout fetches the page then). A source
        ID the link doesn't carry becomes the posting ID, so Job.key == Listing.key."""
        from appa.core.jobid import job_key

        feed_id = self.ats_job_id if self.ats_job_id and not job_key(self.url) else None
        return Job(
            company=self.company,
            title=self.title,
            url=self.url,
            location=self.location,
            description=self.description,
            ats=self.ats if self.ats in KNOWN_ATS else "unknown",
            ats_job_id=self.ats_job_id,
            source=source,
            posting_id=feed_id,
            posting_id_source="feed" if feed_id else None,
        )


@dataclass(frozen=True)
class FeedOptions:
    """What a run asks of every source. Sources use what applies to them and ignore the rest."""

    queries: tuple[str, ...] = ("software engineer", "new grad software engineer", "entry level software engineer")
    since: date | None = None  # sources that can filter by date return only newer postings
    max_results: int = 2000  # per source, a safety cap
    us_only: bool = True  # search APIs that take a country use the US


@dataclass(frozen=True)
class ListFeed:
    """A source of many companies' postings."""

    name: str  # short id, e.g. "simplify_newgrad"
    description: str
    fetch: Callable[[FeedOptions], list[Listing]]
    describe: Callable[[Listing], str] | None = None  # full posting text, when fetch() doesn't include it
    key_env: str | None = None  # env var with an API key; skipped when unset
    extra_env: tuple[str, ...] = ()  # more env vars the source needs (e.g. an app id)
    terms: str = ""  # usage terms worth knowing (attribution, limits)
    default: bool = True  # included in "all"


@dataclass(frozen=True)
class BoardFeed:
    """One company's job board on an ATS."""

    ats: str  # e.g. "smartrecruiters"
    fetch: Callable[[str, FeedOptions], list[Listing]]  # slug -> that board's open postings
    board_of: Callable[[str], str | None]  # any link -> this ATS's board slug, or None
    describe: Callable[[Listing], str] | None = None  # full posting text, when fetch() doesn't include it
    example: str = ""  # an example slug, for docs and live checks
    terms: str = ""
    hosts: tuple[str, ...] = field(default_factory=tuple)  # hosts it calls, for politeness pacing


LISTS: dict[str, ListFeed] = {}
BOARDS: dict[str, BoardFeed] = {}
LOAD_ERRORS: dict[str, str] = {}
_loaded = False


def register_list(feed: ListFeed) -> ListFeed:
    """Add a list source to the registry (called at import time by each module in feeds/lists)."""
    LISTS[feed.name] = feed
    return feed


def register_board(board: BoardFeed) -> BoardFeed:
    """Add an ATS board source to the registry (called at import time by each module in feeds/boards)."""
    BOARDS[board.ats] = board
    return board


def load_all() -> None:
    """Import every source module once; a broken module is recorded in LOAD_ERRORS, not fatal."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from appa.feeds import boards, lists

    for pkg in (lists, boards):
        for m in pkgutil.iter_modules(pkg.__path__):
            name = f"{pkg.__name__}.{m.name}"
            try:
                importlib.import_module(name)
            except Exception as e:  # one bad source must not stop the others
                LOAD_ERRORS[name] = f"{type(e).__name__}: {e}"[:300]
                log.warning("feed module %s failed to load: %s", name, e)


def lists_registry() -> dict[str, ListFeed]:
    """All list sources, loading them on first use."""
    load_all()
    return LISTS


def boards_registry() -> dict[str, BoardFeed]:
    """All ATS board sources, loading them on first use."""
    load_all()
    return BOARDS


def board_of(url: str) -> tuple[str, str] | None:
    """(ats, slug) of the company board a link belongs to, if any registered ATS recognises it."""
    for ats, b in boards_registry().items():
        try:
            slug = b.board_of(url)
        except Exception:
            continue
        if slug:
            return ats, slug
    return None
