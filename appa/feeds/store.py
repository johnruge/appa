"""The listings inbox and the board rotation.

    sources ──► ingest() ──► listings (new | filtered | duplicate)
                   └──► boards found in every link ──► boards_due() ──► polled like any source
    listings(new) ──► next_to_screen() ──► scout screens them ──► mark_screened()

Everything here is plain SQL through the ORM (bound parameters), batched so a 3,000-row source
is a handful of queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from appa import db, observe
from appa.config import Config
from appa.feeds import Listing, board_of
from appa.feeds.filter import reason_to_skip

MAX_DESCRIPTION = 100_000
DISABLE_AFTER = 5  # consecutive failed polls before a board is switched off
_LINK = re.compile(r"https?://\S+")


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


@dataclass
class Ingested:
    """What one batch did to the inbox."""

    seen: int = 0
    new: int = 0
    filtered: int = 0
    duplicate: int = 0
    updated: int = 0
    boards_found: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """One line for the run log."""
        top = ", ".join(f"{r} {n}" for r, n in sorted(self.reasons.items(), key=lambda x: -x[1])[:3])
        return (
            f"{self.seen} listed: {self.new} new, {self.filtered} filtered, {self.duplicate} already known, "
            f"{self.updated} seen before; {self.boards_found} new boards" + (f" (filtered: {top})" if top else "")
        )


def _chunks(xs: list, n: int = 1000):
    """Split a list into IN-clause sized pieces."""
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def _known_keys(s: Session, keys: list[str]) -> dict[str, str]:
    """Keys already tracked as applications or screened before, with how they're known."""
    out: dict[str, str] = {}
    for part in _chunks(keys):
        for k, status in s.execute(
            select(db.Application.job_key, db.Application.status).where(db.Application.job_key.in_(part))
        ):
            out[k] = f"already tracked ({status or 'no status'})"
        for (k,) in s.execute(select(db.SeenJob.key).where(db.SeenJob.key.in_(part))):
            out.setdefault(k, "screened on an earlier run")
    return out


def _reason_bucket(reason: str) -> str:
    """Group filter reasons for the summary ('posted 60 days ago' -> 'posted ... days ago')."""
    return re.sub(r"\d+", "N", reason.split(":")[0].split("(")[0]).strip()[:60]


def ingest(
    s: Session, listings: list[Listing], source: str, cfg: Config, *, today: date | None = None, discover: bool = True
) -> Ingested:
    """Add a source's listings to the inbox (filtered, deduped, boards discovered); commits."""
    today = today or date.today()
    st = Ingested(seen=len(listings))
    by_key: dict[str, Listing] = {}
    for x in listings:
        by_key.setdefault(x.key, x)
    keys = list(by_key)
    existing = {}
    for part in _chunks(keys):
        existing.update({r.key: r for r in s.scalars(select(db.FeedListing).where(db.FeedListing.key.in_(part)))})
    known = _known_keys(s, [k for k in keys if k not in existing])
    now = _now()
    for key, x in by_key.items():
        row = existing.get(key)
        if row:
            st.updated += 1
            row.last_seen_at = now
            if source not in row.sources:
                row.sources = [*row.sources, source][:20]
            if row.status == "new" and not row.description and x.description:
                row.description = x.description[:MAX_DESCRIPTION]
            row.posted_at = row.posted_at or x.posted_at
            row.hints = {**x.hints, **row.hints}
            continue
        status, reason = "new", None
        if key in known:
            status, reason = "duplicate", known[key]
        elif skip := reason_to_skip(x, cfg, today):
            status, reason = "filtered", skip
            st.reasons[_reason_bucket(skip)] = st.reasons.get(_reason_bucket(skip), 0) + 1
        setattr(st, status, getattr(st, status) + 1)
        s.add(
            db.FeedListing(
                key=key,
                company=x.company,
                title=x.title,
                location=x.location,
                url=x.url,
                sources=[source],
                source_ref=x.source_ref,
                ats=x.ats,
                ats_job_id=x.ats_job_id,
                posted_at=x.posted_at,
                description=x.description[:MAX_DESCRIPTION] if status == "new" else "",
                hints=x.hints,
                status=status,
                reason=reason,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    if discover and cfg.feeds.discover_boards:
        st.boards_found = discover_boards(s, listings, found_via=source, commit=False)
    s.commit()
    return st


def discover_boards(s: Session, listings: list[Listing], found_via: str, *, commit: bool = True) -> int:
    """Store the company board behind every link in these listings (and their hints.links); returns how many
    are new."""
    found: dict[tuple[str, str], str] = {}
    for x in listings:
        for url in [x.url, *_LINK.findall(x.hints.get("links", ""))]:
            if b := board_of(url):
                found.setdefault(b, x.company)
    return add_boards(s, found, found_via, commit=commit)


def add_boards(s: Session, boards: dict[tuple[str, str], str], found_via: str, *, commit: bool = True) -> int:
    """Insert boards that aren't stored yet ({(ats, slug): company}); returns how many were new."""
    if not boards:
        return 0
    rows = [
        {
            "ats": a,
            "slug": slug[:200],
            "company": (c or "")[:300],
            "found_via": found_via[:64],
            "enabled": True,
            "kept_total": 0,
            "failures": 0,
            "created_at": _now(),
        }
        for (a, slug), c in boards.items()
    ]
    n = 0
    for part in _chunks(rows, 500):
        res = s.execute(
            insert(db.Board).values(part).on_conflict_do_nothing(constraint="uq_boards_ats_slug").returning(db.Board.id)
        )
        n += len(res.all())
    if commit:
        s.commit()
    return n


def seed_boards(s: Session, cfg: Config) -> int:
    """Boards from your tracker's links and the policy's company lists; returns how many were new."""
    found: dict[tuple[str, str], str] = {}
    for link, company in s.execute(
        select(db.Application.link, db.Application.company).where(db.Application.link.is_not(None))
    ):
        if b := board_of(link):
            found.setdefault(b, company)
    for ats in ("greenhouse", "lever", "ashby"):
        for slug in getattr(cfg.companies, ats):
            found.setdefault((ats, slug), slug)
    return add_boards(s, found, "tracker/config")


def boards_due(s: Session, limit: int, ats: set[str] | None = None) -> list[db.Board]:
    """Enabled boards to poll next: never polled first, then the longest since their last poll."""
    q = select(db.Board).where(db.Board.enabled.is_(True))
    if ats:
        q = q.where(db.Board.ats.in_(sorted(ats)))
    return list(s.scalars(q.order_by(db.Board.last_polled_at.asc().nulls_first(), db.Board.id).limit(limit)))


def board_polled(
    s: Session, board: db.Board, *, count: int | None = None, kept: int = 0, error: str | None = None
) -> None:
    """Record a poll; a board that keeps failing (or doesn't exist) is switched off. Commits."""
    board.last_polled_at = _now()
    if error is None:
        board.failures, board.last_error, board.last_count = 0, None, count
        board.kept_total += kept
    else:
        board.failures += 1
        board.last_error = error[:500]
        if board.failures >= DISABLE_AFTER or (re.search(r"\b404\b", error) and board.failures >= 2):
            board.enabled = False
            observe.record(
                s,
                "board_dead",
                f"{board.ats}:{board.slug}",
                site=board.ats,
                detail={"error": error[:300], "failures": board.failures},
            )
    s.commit()


def next_to_screen(s: Session, limit: int) -> list[db.FeedListing]:
    """New listings to screen next: newest postings first, undated ones after."""
    newest = func.coalesce(db.FeedListing.posted_at, func.date(db.FeedListing.first_seen_at))
    return list(
        s.scalars(
            select(db.FeedListing)
            .where(db.FeedListing.status == "new")
            .order_by(newest.desc(), db.FeedListing.id)
            .limit(limit)
        )
    )


def as_listing(row: db.FeedListing) -> Listing:
    """The stored row as a Listing again (for screening)."""
    return Listing(
        company=row.company,
        title=row.title,
        url=row.url,
        location=row.location,
        posted_at=row.posted_at,
        description=row.description,
        ats=row.ats,
        ats_job_id=row.ats_job_id,
        source_ref=row.source_ref,
        hints=row.hints or {},
    )


def mark(
    s: Session, row: db.FeedListing, status: str, reason: str | None = None, application_id: int | None = None
) -> None:
    """Move a listing to a new status (its stored text is dropped once it leaves the inbox); commits."""
    if status not in db.LISTING_STATUSES:
        raise ValueError(f"unknown listing status {status}")
    row.status, row.reason = status, (reason or "")[:2000] or None
    row.application_id = application_id or row.application_id
    if status != "new":
        row.description = ""
        row.screened_at = _now() if status == "screened" else row.screened_at
    s.commit()


def search(
    s: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[db.FeedListing]:
    """Listings for `appa listings` and the API: filter by status, source and a company/title search."""
    stmt = select(db.FeedListing)
    if status:
        stmt = stmt.where(db.FeedListing.status == status)
    if source:
        stmt = stmt.where(db.FeedListing.sources.contains([source]))
    if q:
        term = q.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{term}%"  # a bound parameter; wildcards in your text are escaped
        stmt = stmt.where(
            or_(db.FeedListing.company.ilike(like, escape="\\"), db.FeedListing.title.ilike(like, escape="\\"))
        )
    return list(
        s.scalars(
            stmt.order_by(db.FeedListing.posted_at.desc().nulls_last(), db.FeedListing.id.desc())
            .offset(offset)
            .limit(min(limit, 500))
        )
    )


def refilter(s: Session, cfg: Config, *, today: date | None = None) -> int:
    """Re-apply the free filters to listings still waiting (e.g. after you change your title rules); returns how
    many were filtered out now. Commits."""
    today = today or date.today()
    n = 0
    for row in s.scalars(select(db.FeedListing).where(db.FeedListing.status == "new")):
        if reason := reason_to_skip(as_listing(row), cfg, today):
            row.status, row.reason, row.description = "filtered", reason[:2000], ""
            n += 1
    s.commit()
    return n
