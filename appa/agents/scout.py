"""Scout: finds postings (links, screenshots, company boards, every job source in appa/feeds) and
screens them.

A sweep (`sweep: true`, what the schedule runs) does three things, each bounded by the policy:
1. reads every list source (SimplifyJobs, job APIs, remote boards...) into the listings inbox, where
   free filters drop what clearly doesn't fit and every link's company board is remembered;
2. polls the next batch of those company boards (least recently polled first) into the inbox;
3. screens the newest inbox listings (model, or keywords when there's no model key).

Every fetch is a recorded tool call; every model call is charged to the run's budget.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from appa import db, extract, feeds, observe
from appa.agents import screener
from appa.agents.base import RoleSpec
from appa.feeds import store
from appa.runtime.context import RunContext, StopRun
from appa.sources import resolve
from appa.sources.fetch import fetch_posting

PACE_S = 0.25  # pause between company boards (they often share one API host)
# ATSs that rate-limit across all their customers' subdomains: at least this long between their boards.
PACE_BY_ATS = {"eightfold": 2.0, "workable": 1.0, "smartrecruiters": 0.5, "workday": 0.5}


class ScoutInput(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=200)
    # Local file paths, CLI only. M4: the API must accept upload ids instead, never server paths.
    screenshots: list[str] = Field(default_factory=list, max_length=20)
    upload_ids: list[int] = Field(default_factory=list, max_length=20)  # screenshots uploaded via the API
    boards: list[str] = Field(default_factory=list, max_length=500)  # "<ats>:slug" or "config"
    sweep: bool = False  # lists + discovered boards + screen the inbox, all sized by the policy
    lists: list[str] = Field(default_factory=list, max_length=100)  # list sources by name, or "all"
    poll_boards: int | None = Field(None, ge=0, le=5000)  # discovered boards to poll
    board_ats: list[str] = Field(default_factory=list, max_length=50)  # only these ATSs' boards
    screen_inbox: int | None = Field(None, ge=0, le=2000)  # inbox listings to screen
    screenshot_model: str | None = None
    max_new: int = Field(200, ge=1, le=2000)  # cap on postings screened
    force: bool = False  # re-screen links already evaluated

    @field_validator("boards")
    @classmethod
    def _boards(cls, v: list[str]) -> list[str]:
        """Reject boards that are neither 'config' nor '<ats>:<slug>' for a registered ATS."""
        known = feeds.boards_registry()
        for b in v:
            if b != "config" and (b.split(":", 1)[0] not in known or ":" not in b):
                raise ValueError(f"board {b!r} must be 'config' or '<ats>:<slug>' with ats one of {sorted(known)}")
        return v

    @field_validator("lists")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        """Reject unknown list source names."""
        known = feeds.lists_registry()
        for name in v:
            if name != "all" and name not in known:
                raise ValueError(f"unknown list source {name!r}; known: {', '.join(sorted(known))}")
        return v


def _board_list(params: ScoutInput) -> list[tuple[str, str]]:
    """Expand the requested boards ('config' means every board in the policy) into unique (kind, slug) pairs."""
    out: list[tuple[str, str]] = []
    for b in params.boards:
        if b == "config":
            companies = screener.load_policy().companies
            out += [(k, slug) for k in ("greenhouse", "lever", "ashby") for slug in getattr(companies, k)]
        else:
            kind, slug = b.split(":", 1)
            out.append((kind, slug))
    return list(dict.fromkeys(out))


def run(ctx: RunContext, params: ScoutInput) -> dict:
    """Screen pasted links, screenshots/uploads and job boards until max_new new jobs; asks you about unresolved
    cards."""
    pipe = screener.make_pipeline(ctx, ctx.model)
    results = []
    budget_left = params.max_new

    def take(job, source: str, force: bool = False) -> None:
        """Screen one job if budget remains; only jobs not already seen use up the budget."""
        nonlocal budget_left
        if budget_left <= 0:
            return
        job.source = source
        res = screener.screen(ctx, pipe, job, force)
        results.append(res)
        if not res.skipped:
            budget_left -= 1

    for url in params.urls:
        try:
            job = ctx.tool("fetch_posting", fetch_posting, url, summarize=lambda j: f"{j.company} / {j.title}")
        except StopRun:
            raise
        except Exception:
            continue
        take(job, "link", params.force)

    unresolved = 0
    already_seen = 0
    shots = list(params.screenshots)
    if params.upload_ids:
        with ctx.sessions() as s:
            for uid in params.upload_ids:
                row = s.get(db.Upload, uid)
                if row:
                    shots.append(row.path)
                else:
                    ctx.log(f"upload #{uid} not found; skipped")
    for shot in shots:
        ctx.check()
        try:
            cards, meta = extract.read_screenshot_with_meta(Path(shot), params.screenshot_model)
        except extract.ExtractionError as e:
            ctx.charge(cost_usd=e.cost_usd, purpose="read screenshot", error=str(e))
            if e.fatal:
                raise
            continue
        ctx.charge(meta, purpose=f"read screenshot: {Path(shot).name}")
        ctx.log(f"{len(cards)} job cards in {Path(shot).name}")
        for card in cards:
            matches, note = ctx.tool(
                "resolve_posting",
                resolve.resolve,
                card.company,
                card.title,
                card.location,
                summarize=lambda r: f"{len(r[0])} match(es): {r[1]}",
            )
            if len(matches) == 1:
                take(matches[0], "screenshot")
                continue
            unresolved += 1
            options = [{"title": m.title, "location": m.location, "url": m.url} for m in matches[:5]]
            ctx.ask(
                f"Which posting is {card.company} / {card.title} ({card.location})? {note}",
                kind="answer_needed",
                context={"card": card.model_dump(), "options": options},
            )

    opts = _options(pipe.cfg)
    for kind, slug in _board_list(params):
        if budget_left <= 0:
            ctx.log(f"reached max_new={params.max_new}; stopping board scan")
            break
        board = feeds.boards_registry()[kind]
        try:
            listed = ctx.tool("fetch_board", board.fetch, slug, opts, summarize=lambda xs: f"{len(xs)} open")
        except StopRun:
            raise
        except Exception:
            continue
        with ctx.sessions() as s:
            seen = set(s.scalars(select(db.SeenJob.key).where(db.SeenJob.key.in_([x.key for x in listed]))))
        fresh = [x for x in listed if x.key not in seen]
        already_seen += len(listed) - len(fresh)
        ctx.log(f"{kind}/{slug}: {len(listed)} open, {len(fresh)} new")
        for x in fresh:
            if budget_left <= 0:
                break
            job = _job(ctx, x, [f"board:{kind}"])
            if job:
                take(job, "poll")

    # ---- every other source: into the inbox, then screen the newest -------------------------
    cfg = pipe.cfg
    names = list(params.lists) or (list(cfg.feeds.lists) if params.sweep else [])
    if names:
        _read_lists(ctx, names, cfg, opts)
    n_boards = (
        params.poll_boards if params.poll_boards is not None else (cfg.feeds.boards_per_run if params.sweep else 0)
    )
    if n_boards:
        _poll_boards(ctx, n_boards, set(params.board_ats) or None, cfg, opts)
    wanted = (
        params.screen_inbox if params.screen_inbox is not None else (cfg.feeds.screen_per_run if params.sweep else 0)
    )
    n_screen = min(wanted, budget_left)
    inbox = {"screened": 0, "errors": 0}
    if n_screen:
        with ctx.sessions() as s:
            rows = store.next_to_screen(s, n_screen)
        ctx.log(f"screening {len(rows)} new listings from the inbox")
        for row in rows:
            _screen_row(ctx, row, take, results, inbox)

    summary = screener.summarize(results)
    if already_seen:
        summary["counts"]["already_seen"] = summary["counts"].get("already_seen", 0) + already_seen
    summary["unresolved_screenshot_cards"] = unresolved
    if n_screen:
        summary["inbox"] = inbox
    return summary


# ---- sources -> inbox ---------------------------------------------------------------------------


def _options(cfg) -> feeds.FeedOptions:
    """What every source is asked for, from the policy."""
    f = cfg.feeds
    since = date.today() - timedelta(days=f.max_age_days) if f.max_age_days else None
    return feeds.FeedOptions(queries=tuple(f.queries), since=since, us_only=f.us_only)


def _missing_env(feed: feeds.ListFeed) -> list[str]:
    """Environment variables this source needs that aren't set."""
    from appa.config import load_env

    load_env()
    return [v for v in (feed.key_env, *feed.extra_env) if v and not os.environ.get(v)]


def _read_lists(ctx: RunContext, names: list[str], cfg, opts: feeds.FeedOptions) -> None:
    """Read each named list source into the inbox; a failing source is logged and recorded, not fatal."""
    registry = feeds.lists_registry()
    chosen = [n for n in names if n != "all"]  # "all" = every default source, plus any others you name
    if "all" in names:
        chosen = sorted({n for n, f in registry.items() if f.default} | set(chosen))
    for name in chosen:
        feed = registry[name]
        if missing := _missing_env(feed):
            ctx.log(f"{name}: skipped (set {', '.join(missing)} to use it)")
            continue
        try:
            listed = ctx.tool(f"list:{name}", feed.fetch, opts, summarize=lambda xs: f"{len(xs)} listed")
        except StopRun:
            raise
        except Exception as e:
            _source_failed(ctx, name, e)
            continue
        try:
            with ctx.sessions() as s:
                st = store.ingest(s, listed, name, cfg)
        except Exception as e:
            _source_failed(ctx, name, e)
            continue
        ctx.log(f"{name}: {st.summary()}")


def _poll_boards(ctx: RunContext, limit: int, only: set[str] | None, cfg, opts: feeds.FeedOptions) -> None:
    """Poll the next `limit` discovered company boards into the inbox."""
    registry = feeds.boards_registry()
    with ctx.sessions() as s:
        due = [(b.id, b.ats, b.slug) for b in store.boards_due(s, limit, only) if b.ats in registry]
    ctx.log(f"polling {len(due)} company boards")
    totals = {"new": 0, "boards": 0}
    last: dict[str, float] = {}
    for i, (bid, ats_name, slug) in enumerate(due):
        if i:
            gap = PACE_BY_ATS.get(ats_name, PACE_S) if PACE_S else 0
            time.sleep(max(PACE_S, gap - (time.monotonic() - last.get(ats_name, 0.0))))
        last[ats_name] = time.monotonic()
        try:
            listed = ctx.tool("board", registry[ats_name].fetch, slug, opts, summarize=lambda xs: f"{len(xs)} open")
        except StopRun:
            raise
        except Exception as e:
            with ctx.sessions() as s:
                store.board_polled(s, s.get(db.Board, bid), error=f"{type(e).__name__}: {e}"[:500])
            continue
        try:
            with ctx.sessions() as s:
                st = store.ingest(s, listed, f"board:{ats_name}", cfg)
                store.board_polled(s, s.get(db.Board, bid), count=len(listed), kept=st.new)
        except Exception as e:
            with ctx.sessions() as s:
                store.board_polled(s, s.get(db.Board, bid), error=f"storing: {type(e).__name__}: {e}"[:500])
            continue
        totals["new"] += st.new
        totals["boards"] += 1
        if st.new:
            ctx.log(f"{ats_name}/{slug}: {st.summary()}")
    ctx.log(f"boards: {totals['boards']} polled, {totals['new']} new listings")


def _source_failed(ctx: RunContext, name: str, e: Exception) -> None:
    """Log a failed source and record it for `appa insights`."""
    msg = f"{type(e).__name__}: {e}"[:300]
    ctx.log(f"{name}: failed ({msg})")
    with ctx.sessions() as s:
        observe.record(s, "feed_error", msg[:200], site=name, detail={"source": name})
        s.commit()


# ---- inbox -> screened --------------------------------------------------------------------------


def _describer(sources: list[str], url: str = ""):
    """A full-text hook for a listing: one of its sources' own, else the hook of the ATS board its link is on
    (one request for that posting, instead of searching the company's whole board)."""
    for src in sources:
        if src.startswith("board:"):
            hook = getattr(feeds.boards_registry().get(src[6:]), "describe", None)
        else:
            hook = getattr(feeds.lists_registry().get(src), "describe", None)
        if hook:
            return hook
    if url and (found := feeds.board_of(url)):
        return getattr(feeds.boards_registry().get(found[0]), "describe", None)
    return None


def _job(ctx: RunContext, x: feeds.Listing, sources: list[str]):
    """The Job to screen for a listing: its own text, the source's describe() hook, or the fetched page.
    None when no text could be had (recorded for insights)."""
    job = x.to_job(sources[0] if sources else "feed")
    if job.description:
        return job
    hook = _describer(sources, x.url)
    if hook:
        try:
            job.description = ctx.tool("describe", hook, x, summarize=lambda d: f"{len(d)} chars") or ""
        except StopRun:
            raise
        except Exception:
            job.description = ""  # fall back to fetching the posting below
    try:
        if not job.description:
            page = ctx.tool("fetch_posting", fetch_posting, x.url, summarize=lambda j: f"{j.company} / {j.title}")
            if page.ats != "unknown":
                page.source = job.source
                return page  # an ATS posting: its API data is the best there is
            job.description = page.description
            job.posting_id = job.posting_id or page.posting_id
            job.posting_id_source = job.posting_id_source or page.posting_id_source
    except StopRun:
        raise
    except Exception as e:
        with ctx.sessions() as s:
            observe.record(
                s, "describe_failed", f"{type(e).__name__}: {e}"[:200], url=x.url, detail={"sources": ",".join(sources)}
            )
            s.commit()
        return None
    return job if job.description else None


def _screen_row(ctx: RunContext, row: db.FeedListing, take, results: list, inbox: dict) -> None:
    """Screen one inbox listing and record what happened to it."""
    job = _job(ctx, store.as_listing(row), list(row.sources))
    with ctx.sessions() as s:
        live = s.get(db.FeedListing, row.id)
        if job is None:
            store.mark(s, live, "error", "couldn't get the posting text; open the link yourself")
            inbox["errors"] += 1
            return
    before = len(results)
    take(job, f"feed:{row.sources[0] if row.sources else 'inbox'}")
    if len(results) == before:
        return  # run budget reached
    res = results[-1]
    with ctx.sessions() as s:
        live = s.get(db.FeedListing, row.id)
        app_id = res.application_id or s.scalar(select(db.Application.id).where(db.Application.job_key == res.job.key))
        if res.skipped:
            store.mark(s, live, "duplicate", "screened on an earlier run", app_id)
        else:
            why = "; ".join(res.decision.reasons[:2])
            store.mark(s, live, "screened", f"{res.decision.verdict.value}: {why}"[:2000], app_id)
            inbox["screened"] += 1


ROLE = RoleSpec(
    name="scout",
    description="Find postings from links, screenshots and company boards, and screen them.",
    Input=ScoutInput,
    run=run,
    default_budget={"max_usd": 1.00, "max_steps": 3000, "max_model_calls": 900},
)
