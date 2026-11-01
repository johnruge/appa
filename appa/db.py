"""Database: the source of truth for applications, every evaluated posting, and API keys.

PostgreSQL only. The app connects as a least-privilege role (APPA_DATABASE_URL:
read/write rows, no DDL); the schema is created and changed only by Alembic
migrations, which run as the owner role (APPA_MIGRATIONS_DATABASE_URL).
See deploy/postgres/roles.sql and migrations/.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from appa.config import load_env
from appa.core import jobid, normalize

STATUSES = (
    "To Apply",
    "Apply Myself",
    "Needs Review",  # discovered, not applied yet
    "Applied",
    "Interviewing",
    "Offer",
    "Rejected",
    "Ghosted",
    "Withdrawn",
    "Skipped",  # discovered, and you chose not to apply
)
APPLIED_STATUSES = {"Applied", "Interviewing", "Offer", "Rejected", "Ghosted", "Withdrawn"}


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    stage: Mapped[str | None] = mapped_column(String(100))
    link: Mapped[str | None] = mapped_column(String(2000))
    location: Mapped[str | None] = mapped_column(String(300))
    date_applied: Mapped[date | None] = mapped_column(Date)
    applied_with_email: Mapped[str | None] = mapped_column(String(254))
    referral: Mapped[bool | None] = mapped_column(Boolean)
    people: Mapped[str | None] = mapped_column(String(500))
    todo: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="manual")  # csv-backfill | api:<key name> | pipeline
    # A job ID that isn't in the link: from the page's structured data, an AI read (verified
    # verbatim against the page), you or your agent, or your notes.
    job_id: Mapped[str | None] = mapped_column(String(100))
    job_id_source: Mapped[str | None] = mapped_column(String(32))
    # The posting's unique identity (see core/jobid.py: resolve_key). Null when nothing
    # identifies it (dashboards, Workday "completed" pages, rows without a link or ID).
    job_key: Mapped[str | None] = mapped_column(String(300), unique=True)
    # Dedupe keys, derived from the raw fields on every write.
    company_norm: Mapped[str] = mapped_column(String(200), index=True)
    role_norm: Mapped[str] = mapped_column(String(300))
    import_key: Mapped[str | None] = mapped_column(String(64), unique=True)  # makes backfill re-runnable
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    def refresh_keys(self) -> None:
        """Recompute company_norm, role_norm and job_key from the current fields."""
        self.company_norm = normalize.company(self.company)
        self.role_norm = normalize.title(self.role)
        self.job_key = jobid.resolve_key(self.link, self.job_id, self.company)

    def to_dict(self) -> dict:
        """The application as a JSON-ready dict, dates as ISO strings."""
        cols = (
            "id",
            "company",
            "role",
            "status",
            "stage",
            "link",
            "location",
            "date_applied",
            "applied_with_email",
            "referral",
            "people",
            "todo",
            "notes",
            "source",
            "job_id",
            "job_id_source",
            "job_key",
            "created_at",
            "updated_at",
        )
        out = {c: getattr(self, c) for c in cols}
        for k in ("date_applied", "created_at", "updated_at"):
            out[k] = out[k].isoformat() if out[k] else None
        return out


@event.listens_for(Application, "before_insert")
@event.listens_for(Application, "before_update")
def _keys(_mapper, _conn, target: Application) -> None:
    """Before insert/update, recompute an application's normalized names and job key."""
    target.refresh_keys()


class SeenJob(Base):
    """Every posting the pipeline evaluated, including rejects."""

    __tablename__ = "seen_jobs"

    key: Mapped[str] = mapped_column(String(2000), primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000))
    source: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(32), index=True)
    reasons: Mapped[list] = mapped_column(JSONB)
    extraction: Mapped[dict | None] = mapped_column(JSONB)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    """One key per agent. Only a SHA-256 hash of the token is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # shown in listings so you can tell keys apart
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---- runtime: runs, their events, the job queue, schedules, questions ----------------

RUN_STATUSES = ("queued", "running", "waiting_for_you", "succeeded", "failed", "cancelled")
TERMINAL = {"succeeded", "failed", "cancelled"}


class Run(Base):
    """One execution of an agent role. Everything it does is in run_events."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(100))  # None = role default
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    budget: Mapped[dict] = mapped_column(JSONB, default=dict)  # max_usd, max_steps, max_model_calls
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64))  # cli | schedule:<id> | api:<key name>
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    """Append-only log of a run: what the terminal UI streams and the audit trail."""

    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    kind: Mapped[str] = mapped_column(String(20))  # status | log | model_call | tool_call | question | result
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class QueueJob(Base):
    """Work for workers. Claimed with SELECT ... FOR UPDATE SKIP LOCKED; leases expire."""

    __tablename__ = "queue_jobs"
    __table_args__ = (Index("ix_queue_jobs_ready", "status", "available_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # run (workers) | apply (your browser runner)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)  # apply: {"attempt_id", "phase"}
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | leased | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    cron: Mapped[str] = mapped_column(String(100))  # standard 5-field cron, UTC
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    budget: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Question(Base):
    """Something a run needs from you (review items now; Applier questions in M6)."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # review | answer_needed
    prompt: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | answered | dismissed
    answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """Small runtime switches, e.g. paused (the kill switch)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# ---- people: your login, browser sessions, uploads, profile history ------------------


class User(Base):
    """Humans who log into the web UI (v1: just you). Agents use ApiKey instead."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id
    totp_secret: Mapped[str | None] = mapped_column(String(64))  # base32; set = 2FA required
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebSession(Base):
    """A browser login. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class Upload(Base):
    """A validated, re-encoded image (screenshots). Files live outside any web root."""

    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)  # of the re-encoded file
    mime: Mapped[str] = mapped_column(String(32))
    bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProfileVersion(Base):
    """Every saved version of profile.md (judgment) and the policy (hard rules).
    The newest of each kind is in effect; older ones show what a past decision used."""

    __tablename__ = "profile_versions"
    __table_args__ = (Index("ix_profile_versions_kind_id", "kind", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # profile_md | policy
    content: Mapped[dict] = mapped_column(JSONB)  # {"text": ...} or the policy object
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---- prefilling: your answers, and each attempt to fill an application for you ----------


class Answer(Base):
    """Your facts for application forms. Standard keys (first_name, requires_sponsorship, ...)
    and saved answers to specific questions ("q:<normalized question>")."""

    __tablename__ = "answers"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="you")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


APPLY_STATUSES = (
    "preparing",  # the runner is reading or filling the form
    "needs_you",  # questions only you can answer
    "ready",  # filled and left open in your browser: you check it and press submit
    "applied",  # you submitted it and said so
    "failed",
    "cancelled",
    "submitted",  # history only: from before appa stopped submitting
    "confirmed",  # history only
)


class ApplyAttempt(Base):
    """One application prefilled in your browser by the runner. appa never submits: the filled form is
    left open for you, and it becomes Applied only when you say you submitted it."""

    __tablename__ = "apply_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    ats: Mapped[str | None] = mapped_column(String(32))
    apply_url: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="preparing", index=True)
    form: Mapped[list | None] = mapped_column(JSONB)  # questions the runner found
    plan: Mapped[list | None] = mapped_column(JSONB)  # what goes in each field, and why
    fill_report: Mapped[list | None] = mapped_column(JSONB)  # per-field result of the last fill
    preview_upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    confirmation_upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    final_url: Mapped[str | None] = mapped_column(String(2000))
    error: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


LISTING_STATUSES = ("new", "filtered", "duplicate", "screened", "skipped", "error")


class FeedListing(Base):
    """A posting some source listed (appa/feeds), before or after screening. One row per job ID:
    the same posting from several sources is one row with several `sources`."""

    __tablename__ = "listings"
    __table_args__ = (Index("ix_listings_status_posted", "status", "posted_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(500), unique=True)
    company: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(String(2000))
    sources: Mapped[list] = mapped_column(
        JSONB, default=list
    )  # feed names, e.g. ["simplify_newgrad", "board:greenhouse"]
    source_ref: Mapped[str | None] = mapped_column(String(300))
    ats: Mapped[str] = mapped_column(String(32), default="unknown")
    ats_job_id: Mapped[str | None] = mapped_column(String(100))
    posted_at: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text, default="")  # cleared once screened or filtered
    hints: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Board(Base):
    """A company's job board on an ATS, polled in rotation. Found from links in any source."""

    __tablename__ = "boards"
    __table_args__ = (UniqueConstraint("ats", "slug", name="uq_boards_ats_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ats: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(300), default="")
    found_via: Mapped[str] = mapped_column(String(64), default="you")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_count: Mapped[int | None] = mapped_column(Integer)  # open postings at the last poll
    kept_total: Mapped[int] = mapped_column(Integer, default=0)  # postings that passed the free filters, ever
    failures: Mapped[int] = mapped_column(Integer, default=0)  # consecutive failed polls
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Observation(Base):
    """Something that didn't work automatically (see appa/observe.py), kept to improve appa."""

    __tablename__ = "observations"
    __table_args__ = (Index("ix_observations_kind_created", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    site: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("apply_attempts.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def database_url(var: str = "APPA_DATABASE_URL") -> str:
    """The PostgreSQL URL from the given env var (after loading .env); exits if it isn't PostgreSQL."""
    load_env()
    url = os.environ.get(var, "")
    if not url.startswith("postgresql"):
        raise SystemExit(
            f"{var} must be a PostgreSQL URL (postgresql+psycopg://...). "
            "For local development run scripts/dev-db.sh init, which writes .env."
        )
    return url


def make_engine(url: str | None = None) -> Engine:
    """An engine with a bounded pool, connect and statement timeouts, and UTC sessions."""
    # Bounded pool, connect timeout, and a per-statement timeout (the app role also has one).
    return create_engine(
        url or database_url(),
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        # UTC sessions: timestamps come back in UTC whatever the server's local timezone is.
        connect_args={"connect_timeout": 10, "options": "-c statement_timeout=10000 -c timezone=UTC"},
    )


_factory: sessionmaker[Session] | None = None


def sessions(url: str | None = None) -> sessionmaker[Session]:
    """The process-wide session factory. Passing a URL replaces it (tests, tools)."""
    global _factory
    if _factory is None or (url and str(_factory.kw["bind"].url.render_as_string(hide_password=False)) != url):
        if _factory is not None:
            _factory.kw["bind"].dispose()
        _factory = sessionmaker(make_engine(url), expire_on_commit=False)
    return _factory


# ---- queries ------------------------------------------------------------------


class Match:
    def __init__(self, kind: str, app: Application | None, others: list[Application] | None = None):
        """A match result: its kind, the matching application, and other related ones."""
        # same_posting: same job ID. The only thing that counts as "already applied".
        # company_applied: you've applied to other postings (with job IDs) at this company.
        # company_tracked: the company is on file, but nothing there has a job ID to compare.
        self.kind = kind
        self.app = app
        self.others = others or []


def find_match(s: Session, company: str, role: str, link: str | None, job_id: str | None = None) -> Match | None:
    """How this job relates to what's on file.

    Only an exact job-ID match blocks. Rows with no link or no job ID are fair game:
    applying twice is better than not applying at all.
    """
    key = jobid.resolve_key(link, job_id, company)
    if key:
        hit = s.scalars(select(Application).where(Application.job_key == key)).first()
        if hit:
            return Match("same_posting", hit)
    c = normalize.company(company)
    if not c:
        return None
    # Same company + same job ID seen on a different site (e.g. an ID from your notes, then the
    # same posting on the company's job site): still the same posting.
    bare = jobid.clean_id(job_id) or (key.rpartition(":")[2] if key else None)
    if bare:
        hit = s.scalars(
            select(Application).where(
                Application.company_norm == c, (Application.job_id == bare) | Application.job_key.like("%:" + bare)
            )
        ).first()
        if hit:
            return Match("same_posting", hit)
    rows = list(s.scalars(select(Application).where(Application.company_norm == c)))
    applied = [a for a in rows if a.job_key and a.status in APPLIED_STATUSES]
    if applied:
        return Match("company_applied", applied[0], rows)
    if rows:
        return Match("company_tracked", rows[0], rows)
    return None
