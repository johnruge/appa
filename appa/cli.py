from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import threading
from pathlib import Path

from sqlalchemy import select

from appa import backfill, db
from appa import config as config_mod
from appa.api import auth
from appa.core.models import Verdict
from appa.sources.fetch import fetch_posting as job_from_url

# ---- runs -------------------------------------------------------------------------


def _budget(args) -> dict:
    """The run budget from --max-usd / --max-steps, only the ones given."""
    b = {}
    if getattr(args, "max_usd", None) is not None:
        b["max_usd"] = args.max_usd
    if getattr(args, "max_steps", None) is not None:
        b["max_steps"] = args.max_steps
    return b


def _daily_cap() -> float | None:
    """The daily spend cap from the model registry limits, or None."""
    from appa.llm import registry as models

    cap = models.get_registry().limits.get("daily_usd_cap")
    return float(cap) if cap is not None else None


def start_run(role: str, input: dict, args, *, inline: bool) -> int:
    """Create a run; execute it here (inline) or leave it for a worker; stream its events."""
    from pydantic import ValidationError

    from appa import llm
    from appa.runtime import render, runs

    sessions = db.sessions()
    with sessions() as s:
        try:
            run = runs.create(
                s,
                role,
                model=getattr(args, "model", None),
                input=input,
                budget=_budget(args),
                created_by="cli",
                enqueue=not inline,
            )
        except (ValidationError, ValueError, llm.ModelNotAllowed) as e:
            raise SystemExit(f"{role}: {e}") from e
        run_id = run.id
    print(f"run #{run_id} ({role}) " + ("running here" if inline else "queued for a worker"))
    if getattr(args, "no_follow", False) and not inline:
        return run_id
    worker = None
    if inline:
        worker = threading.Thread(
            target=runs.execute, args=(sessions, run_id), kwargs={"daily_cap_usd": _daily_cap()}, daemon=True
        )
        worker.start()
    picked_up = inline
    try:
        for ev in runs.follow(sessions, run_id, give_up_if_queued_s=None if inline else 15):
            if ev.kind == "status" and ev.payload.get("status") != "queued":
                picked_up = True
            for line in render.format_event(ev):
                print(line, flush=True)
        if not picked_up:
            print(
                f"no worker picked up run #{run_id} yet. Start one with `appa worker`, "
                f"or rerun with --inline. Follow later with `appa runs {run_id}`."
            )
    except KeyboardInterrupt:
        with sessions() as s:
            runs.cancel(s, run_id)
        print(f"\ncancel requested for run #{run_id}")
    if worker:
        worker.join(timeout=30)
    return run_id


def cmd_add(args) -> None:
    """`appa add`: screen job links in a Scout run."""
    start_run("scout", {"urls": args.urls, "force": args.force}, args, inline=not args.queue)


def cmd_screenshot(args) -> None:
    """`appa screenshot`: read job cards from screenshot files (must exist) and screen them."""
    files = [str(Path(f).resolve()) for f in args.images]
    for f in files:
        if not Path(f).is_file():
            raise SystemExit(f"no such file: {f}")
    start_run("scout", {"screenshots": files, "screenshot_model": args.screenshot_model}, args, inline=not args.queue)


def cmd_poll(args) -> None:
    """`appa poll`: check job boards (all configured ones by default) for new postings."""
    start_run("scout", {"boards": args.board or ["config"], "max_new": args.max_new}, args, inline=not args.queue)


def cmd_sweep(args) -> None:
    """`appa sweep`: read every job source, poll discovered company boards, screen the newest (the scheduled job)."""
    data: dict = {"sweep": True, "max_new": args.max_new}
    if args.lists:
        data["lists"] = args.lists.split(",")
    if args.boards is not None:
        data["poll_boards"] = args.boards
    if args.screen is not None:
        data["screen_inbox"] = args.screen
    if args.no_screen:
        data["screen_inbox"], data["sweep"] = 0, False
        data.setdefault("lists", ["all"])
        data.setdefault("poll_boards", 150)
    start_run("scout", data, args, inline=not args.queue)


def cmd_feeds(args) -> None:
    """`appa feeds`: every job source, whether it's ready, and the inbox and board counts."""
    import os

    from sqlalchemy import func

    from appa import feeds
    from appa.config import load_env

    load_env()
    for f in sorted(feeds.lists_registry().values(), key=lambda f: f.name):
        missing = [v for v in (f.key_env, *f.extra_env) if v and not os.environ.get(v)]
        state = f"needs {', '.join(missing)}" if missing else ("ready" if f.default else "ready (not in 'all')")
        print(f"  {f.name:<22} {state:<34} {f.description}"[:150])
    for name, err in feeds.LOAD_ERRORS.items():
        print(f"  {name}: failed to load: {err}")
    with db.sessions()() as s:
        boards = dict(
            s.execute(select(db.Board.ats, func.count()).where(db.Board.enabled.is_(True)).group_by(db.Board.ats)).all()
        )
        inbox = dict(s.execute(select(db.FeedListing.status, func.count()).group_by(db.FeedListing.status)).all())
    print(f"\nboard ATSs: {', '.join(sorted(feeds.boards_registry()))}")
    print(
        "boards in rotation: "
        + (", ".join(f"{k} {v}" for k, v in sorted(boards.items())) or "none yet (run `appa boards seed` or a sweep)")
    )
    print("inbox: " + (", ".join(f"{k} {v}" for k, v in sorted(inbox.items())) or "empty"))


def cmd_listings(args) -> None:
    """`appa listings`: browse the inbox; track, skip or restore listings by id."""
    from appa.feeds import store

    with db.sessions()() as s:
        if args.action == "refilter":
            from appa.agents import screener

            print(f"filtered out {store.refilter(s, screener.load_policy())} waiting listings with your current rules")
            return
        if args.action in ("track", "skip", "restore"):
            for lid in args.ids:
                row = s.get(db.FeedListing, lid)
                if not row:
                    print(f"#{lid}: no such listing")
                    continue
                if args.action == "skip":
                    store.mark(s, row, "skipped", "skipped by you (cli)")
                elif args.action == "restore":
                    if row.status not in ("filtered", "skipped", "error"):
                        print(f"#{lid}: is {row.status}; only filtered, skipped or failed listings can be restored")
                        continue
                    store.mark(s, row, "new", None)
                else:
                    app = _track_listing(s, row)
                    print(f"#{lid} -> application #{app.id} ({app.status}) {app.company} / {app.role}")
                    continue
                print(f"#{lid}: {row.status}")
            return
        rows = store.search(s, status=args.status, source=args.source, q=args.q, limit=args.limit)
        for r in rows:
            when = r.posted_at.isoformat() if r.posted_at else "          "
            why = f"  ({r.reason[:50]})" if r.reason and r.status != "new" else ""
            print(
                f"#{r.id:<6} {when} {r.status:<9} {r.company[:24]:<24} {r.title[:48]:<48} {r.location[:24]}{why}"[:170]
            )
        if not rows:
            print("no listings match")


def _track_listing(s, row: db.FeedListing) -> db.Application:
    """Track a listing as To Apply (or return the application already tracking it)."""
    from appa.feeds import store

    existing = s.scalars(select(db.Application).where(db.Application.job_key == row.key)).first()
    if existing:
        return existing
    job = store.as_listing(row).to_job("listing")
    app = db.Application(
        company=row.company,
        role=row.title,
        location=row.location or None,
        link=row.url,
        status="To Apply",
        source=f"listing:{(row.sources or ['?'])[0]}"[:64],
        job_id=job.posting_id,
        job_id_source=job.posting_id_source,
        notes="picked from the inbox by you (not screened)",
    )
    s.add(app)
    s.flush()
    store.mark(s, row, "screened", "tracked by you (cli)", app.id)
    return app


def cmd_boards(args) -> None:
    """`appa boards`: company boards in the poll rotation; seed them, add one, or switch one off."""
    from appa import feeds
    from appa.agents import screener
    from appa.feeds import store

    with db.sessions()() as s:
        if args.action == "seed":
            print(f"added {store.seed_boards(s, screener.load_policy())} boards from your tracker and policy")
            return
        if args.action == "add":
            for spec in args.specs:
                ats, _, slug = spec.partition(":")
                if ats not in feeds.boards_registry() or not slug:
                    raise SystemExit(
                        f"{spec!r}: use <ats>:<slug>, ats one of {', '.join(sorted(feeds.boards_registry()))}"
                    )
                n = store.add_boards(s, {(ats, slug): slug}, "you")
                print(f"{spec}: {'added' if n else 'already in rotation'}")
            return
        if args.action in ("disable", "enable"):
            for bid in args.specs:
                b = s.get(db.Board, int(bid))
                if b:
                    b.enabled, b.failures = args.action == "enable", 0
            s.commit()
            return
        q = select(db.Board).order_by(db.Board.kept_total.desc(), db.Board.id)
        if args.ats:
            q = q.where(db.Board.ats == args.ats)
        for b in s.scalars(q.limit(args.limit)):
            polled = b.last_polled_at.astimezone().strftime("%m-%d %H:%M") if b.last_polled_at else "never"
            state = "" if b.enabled else f"  OFF ({(b.last_error or '')[:40]})"
            print(
                f"#{b.id:<5} {b.ats:<15} {b.slug[:36]:<36} open {b.last_count if b.last_count is not None else '-':>4}"
                f"  kept {b.kept_total:>4}  polled {polled}{state}"[:160]
            )


def cmd_takeover(args) -> None:
    """`appa takeover`: you apply to these yourself; stops appa's attempt (before submit) and gives you the link."""
    from appa.apply import service

    with db.sessions()() as s:
        for app_id in args.ids:
            try:
                app, link = service.takeover(s, app_id, "user:cli")
            except service.ApplyError as e:
                print(f"#{app_id}: {e}")
                continue
            print(f"#{app.id} {app.company} / {app.role} is yours (Apply Myself): {link or 'no link on file'}")


def cmd_run(args) -> None:
    """`appa run`: start any role with JSON input plus --url/--board/--screenshot shortcuts."""
    import json

    data = json.loads(args.input) if args.input else {}
    if args.url:
        data["urls"] = args.url
    if args.board:
        data["boards"] = args.board
    if args.screenshot:
        data["screenshots"] = [str(Path(f).resolve()) for f in args.screenshot]
    start_run(args.role, data, args, inline=args.inline)


def cmd_runs(args) -> None:
    """`appa runs`: list recent runs, or follow one run's events."""
    from appa.runtime import render, runs

    sessions = db.sessions()
    if args.id is None:
        with sessions() as s:
            for r in s.scalars(select(db.Run).order_by(db.Run.id.desc()).limit(args.limit)):
                when = r.created_at.astimezone().strftime("%m-%d %H:%M")
                print(
                    f"#{r.id:<5} {when}  {r.role:<10} {r.status:<16} ${r.cost_usd:<8.4f} "
                    f"{r.model or 'default':<36} {r.created_by}" + (f"  {r.error[:60]}" if r.error else "")
                )
        return
    for ev in runs.follow(sessions, args.id):
        for line in render.format_event(ev):
            print(line, flush=True)


def cmd_cancel(args) -> None:
    """`appa cancel`: cancel a run and print its status."""
    from appa.runtime import runs

    with db.sessions()() as s:
        run = runs.cancel(s, args.id)
        print(
            f"run #{args.id}: "
            + (
                "not found"
                if not run
                else f"{run.status}"
                + (" (cancel requested)" if run.cancel_requested and run.status == "running" else "")
            )
        )


def cmd_stop_all(args) -> None:
    """`appa stop-all`: pause workers and cancel every active run."""
    from appa.runtime import runs

    with db.sessions()() as s:
        n = runs.stop_all(s)
    print(f"paused: workers won't start new runs. Cancelled {n} active run(s). Undo with `appa resume`.")


def cmd_resume(args) -> None:
    """`appa resume`: unpause workers."""
    from appa.runtime.context import set_paused

    with db.sessions()() as s:
        set_paused(s, False)
    print("resumed: workers will pick up queued runs again")


def cmd_worker(args) -> None:
    """`appa worker`: run queued jobs forever, or just one with --once."""
    from appa.runtime.worker import Worker

    w = Worker(daily_cap_usd=_daily_cap())
    if args.once:
        print("ran 1 job" if w.run_one() else "nothing to do")
    else:
        w.loop()


def cmd_scheduler(args) -> None:
    """`appa scheduler`: run the loop that queues runs for due schedules."""
    from appa.runtime import scheduler

    scheduler.loop()


def cmd_schedule(args) -> None:
    """`appa schedule`: add, list, remove, or turn on/off recurring runs."""
    from appa.runtime import scheduler

    with db.sessions()() as s:
        if args.action == "add":
            import json

            data = json.loads(args.input) if args.input else {}
            if args.board:
                data["boards"] = args.board
            if args.sweep:
                data["sweep"] = True
            try:
                sch = scheduler.add(s, args.role, args.cron, model=args.model, input=data, budget=_budget(args))
            except ValueError as e:
                raise SystemExit(str(e)) from e
            print(f"schedule #{sch.id}: {sch.role} at '{sch.cron}' (UTC), next {sch.next_run_at:%Y-%m-%d %H:%M} UTC")
        elif args.action == "list":
            for sch in s.scalars(select(db.Schedule).order_by(db.Schedule.id)):
                state = "on " if sch.enabled else "off"
                print(
                    f"#{sch.id:<4} {state} {sch.role:<10} '{sch.cron}'  next {sch.next_run_at:%m-%d %H:%M} UTC  "
                    f"{sch.model or 'default'}  {sch.input}"
                )
        elif args.action in ("rm", "on", "off"):
            sch = s.get(db.Schedule, args.id)
            if not sch:
                raise SystemExit(f"no schedule #{args.id}")
            if args.action == "rm":
                s.delete(sch)
            else:
                sch.enabled = args.action == "on"
            s.commit()
            print(f"schedule #{args.id}: {args.action}")


def cmd_questions(args) -> None:
    """`appa questions`: print the open questions."""
    with db.sessions()() as s:
        qs = list(s.scalars(select(db.Question).where(db.Question.status == "open").order_by(db.Question.id)))
        for q in qs:
            print(f"#{q.id:<5} [{q.kind}] {q.prompt[:200]}")
        print(f"{len(qs)} open question(s)")


def cmd_list(args) -> None:
    """`appa list`: print recently evaluated postings and their reasons, optionally by verdict."""
    with db.sessions()() as s:
        stmt = select(db.SeenJob).order_by(db.SeenJob.seen_at.desc()).limit(args.limit)
        if args.verdict:
            stmt = stmt.where(db.SeenJob.verdict == args.verdict)
        for row in s.scalars(stmt):
            print(f"{row.verdict:<10} {row.company} | {row.title} | {row.url}")
            for reason in row.reasons:
                print(f"{'':<11}- {reason[:200]}")


def cmd_backfill(args) -> None:
    """`appa backfill`: import a tracker CSV and warn about likely company-name variants."""
    email = args.email
    if email is None and config_mod.DEFAULT_PATH.exists():
        email = config_mod.load().candidate.application_email
    with db.sessions()() as s:
        rep = backfill.import_csv(s, Path(args.csv), applied_with_email=email)
        print(
            f"imported {rep.imported} rows, skipped {rep.skipped_existing} already imported"
            f" (applied_with_email={email or 'blank'})"
        )
        for w in rep.warnings:
            print(f"  ⚠️  {w}")
        similar = backfill.name_warnings(s)
        if similar:
            print("possible spelling variants of the same company (fix in the DB or sheet if so):")
            for w in similar:
                print(f"  - {w}")


def cmd_export(args) -> None:
    """`appa export`: write every application to a CSV file."""
    cols = [
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
    ]
    with db.sessions()() as s, open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        n = 0
        for a in s.scalars(select(db.Application).order_by(db.Application.id)):
            w.writerow(a.to_dict())
            n += 1
    print(f"wrote {n} applications to {args.csv}")


def cmd_keys(args) -> None:
    """`appa keys`: create (prints the token once), list, or revoke API keys."""
    with db.sessions()() as s:
        if args.action == "create":
            scopes = args.scopes.split(",")
            try:
                token = auth.create_key(s, args.name, scopes)
            except ValueError as e:
                raise SystemExit(str(e)) from e
            print(
                f"API key for {args.name!r} (scopes: {', '.join(scopes)}). Shown once; store it in the agent's secrets:"
            )
            print(token)
        elif args.action == "list":
            for k in s.scalars(select(db.ApiKey).order_by(db.ApiKey.id)):
                state = "revoked" if k.revoked_at else "active"
                used = k.last_used_at.isoformat(timespec="minutes") if k.last_used_at else "never"
                print(f"{k.name:<20} appa_{k.prefix}_…  {state:<8} {','.join(k.scopes):<40} last used {used}")
        elif args.action == "revoke":
            print("revoked" if auth.revoke(s, args.name) else f"no active key named {args.name!r}")


def cmd_user(args) -> None:
    """`appa user`: create a user, change password, turn on TOTP, or unlock an account."""
    import getpass

    from appa.api import users

    def ask_password() -> str:
        """Prompt for a password twice; exits if they differ."""
        pw = getpass.getpass("password (12+ characters): ")
        if pw != getpass.getpass("again: "):
            raise SystemExit("passwords don't match")
        return pw

    with db.sessions()() as s:
        if args.action == "create":
            try:
                u = users.create_user(s, args.username, ask_password())
            except ValueError as e:
                raise SystemExit(str(e)) from e
            print(f"created user {u.username}. Turn on 2FA with `appa user totp {u.username}`.")
            return
        u = s.scalars(select(db.User).where(db.User.username == args.username.lower())).first()
        if not u:
            raise SystemExit(f"no user {args.username!r}")
        if args.action == "passwd":
            try:
                users.set_password(s, u, ask_password())
            except ValueError as e:
                raise SystemExit(str(e)) from e
            print("password changed; all browser sessions were logged out")
        elif args.action == "totp":
            uri = users.enable_totp(s, u)
            print("Add this to your authenticator app (it's a secret: don't share or commit it):")
            print(uri)
            print("Login now requires the 6-digit code.")
        elif args.action == "unlock":
            u.failed_logins, u.locked_until = 0, None
            s.commit()
            print(f"{u.username} unlocked")


def cmd_track(args) -> None:
    """Add postings you picked yourself as "To Apply" (no screening, no model call)."""
    import httpx

    from appa.core.jobid import clean_id, resolve_key

    if args.job_id and len(args.urls) != 1:
        raise SystemExit("--job-id goes with exactly one link")
    if args.job_id and not clean_id(args.job_id):
        raise SystemExit(f"{args.job_id!r} doesn't look like a job ID (letters/digits, must contain a digit)")
    with db.sessions()() as s:
        for url in args.urls:
            key = resolve_key(url, args.job_id)
            existing = s.scalars(select(db.Application).where(db.Application.job_key == key)).first() if key else None
            if existing:
                print(f"#{existing.id}: already tracked ({existing.status}) {existing.company} / {existing.role}")
                continue
            try:
                job = job_from_url(url)
            except (httpx.HTTPError, LookupError, ValueError) as e:
                print(f"could not fetch {url}: {e}")
                continue
            jid, how = (args.job_id, "provided") if args.job_id else (job.posting_id, job.posting_id_source)
            app = db.Application(
                company=job.company,
                role=job.title,
                location=job.location or None,
                link=job.url,
                status="To Apply",
                source="cli:track",
                job_id=jid,
                job_id_source=how,
                notes="added by you with appa track (not screened)",
            )
            s.add(app)
            s.commit()
            print(
                f"#{app.id}: {app.company} / {app.role} ({app.location or 'no location'}) is To Apply. "
                f"Run `appa apply {app.id}` to fill it in your browser."
            )


def cmd_ids(args) -> None:
    """`appa ids`: find job IDs written in notes; saves them only with --apply."""
    with db.sessions()() as s:
        rows = backfill.ids_from_notes(s, apply=args.apply)
    for rid, company, jid, outcome in rows:
        print(f"#{rid:<5} {company[:32]:<32} {jid:<24} {outcome}")
    n = sum(1 for r in rows if r[3] in ("set", "would set"))
    print(
        f"{n} row(s) {'updated' if args.apply else 'would get a job ID'}"
        + ("" if args.apply else "; run with --apply to save")
    )


def cmd_apply(args) -> None:
    """`appa apply`: queue applications for the browser runner, or redo an attempt."""
    from appa.apply import service

    with db.sessions()() as s:
        if args.redo:
            try:
                service.redo(s, args.redo, "user:cli")
            except service.ApplyError as e:
                raise SystemExit(str(e)) from e
            print(f"attempt #{args.redo} queued to be filled again; a new approval card will replace the old one")
            return
        ids = args.ids
        if args.all_to_apply:
            ids = list(s.scalars(select(db.Application.id).where(db.Application.status == "To Apply")))
        for app_id in ids:
            try:
                a = service.start(s, app_id, "cli")
                print(f"#{app_id}: attempt #{a.id} queued for your runner ({a.ats})")
            except service.ApplyError as e:
                print(f"#{app_id}: {e}")


def cmd_attempts(args) -> None:
    """`appa attempts`: print recent apply attempts."""
    with db.sessions()() as s:
        rows = s.scalars(select(db.ApplyAttempt).order_by(db.ApplyAttempt.id.desc()).limit(args.limit))
        for a in rows:
            app = s.get(db.Application, a.application_id)
            print(
                f"#{a.id:<4} {a.status:<18} {app.company} / {app.role}"[:110]
                + (f"  ({a.error[:60]})" if a.error else "")
            )


def cmd_insights(args) -> None:
    """Show the most common misses and failures, i.e. what to teach appa next."""
    import json

    from appa import observe

    with db.sessions()() as s:
        if args.backfill:
            print(f"recorded {observe.backfill(s)} observation(s) from earlier postings and attempts")
            return
        if args.prune:
            print(f"removed {observe.prune(s, args.prune)} observation(s) older than {args.prune} days")
            return
        rows = observe.summary(s, days=args.days, kind=args.kind, limit=args.limit)
        if args.json:
            print(json.dumps(rows, default=str, indent=1))
            return
        totals = observe.counts(s, days=args.days)
        if not totals:
            print(f"nothing recorded in the last {args.days} days")
            return
        print(
            f"last {args.days} days: " + ", ".join(f"{k} {n}" for k, n in sorted(totals.items(), key=lambda x: -x[1]))
        )
        kind = None
        for r in rows:
            if r["kind"] != kind:
                kind = r["kind"]
                print(f"\n{kind}: {observe.KINDS[kind]}")
            print(f"  {r['n']:>3}x  {r['site'] or '-':<28} {r['subject']}"[:140])
        if args.kind and rows:
            ex = s.get(db.Observation, rows[0]["example_id"])
            print(f"\nlatest example: {json.dumps(ex.detail)[:600]}")


def cmd_approve(args) -> None:
    """`appa approve`: approve a filled attempt so the runner submits it."""
    from appa.apply import service

    with db.sessions()() as s:
        try:
            service.approve(s, args.attempt_id, "user:cli")
        except service.ApplyError as e:
            raise SystemExit(str(e)) from e
    print(f"attempt #{args.attempt_id} approved: your runner will submit it")


def cmd_answers(args) -> None:
    """`appa answers`: list, set, remove, or import (from YAML) saved form answers."""
    import yaml

    from appa.apply import service

    with db.sessions()() as s:
        if args.action == "list":
            for k, v in sorted(service.answers(s).items()):
                print(f"{k:<32} {v[:80]}")
        elif args.action == "set":
            service.set_answer(s, args.key, args.value)
            print(f"{args.key} saved")
        elif args.action == "rm":
            row = s.get(db.Answer, args.key)
            if row:
                s.delete(row)
                s.commit()
            print(f"{args.key} removed" if row else f"no answer {args.key!r}")
        elif args.action == "import":
            data = yaml.safe_load(Path(args.file).read_text()) or {}
            n = 0
            for k, v in data.items():
                if v not in (None, ""):
                    service.set_answer(s, str(k), str(v))
                    n += 1
            print(f"imported {n} answers (blank ones skipped)")


def cmd_runner(args) -> None:
    """`appa runner`: run the browser runner on this machine with APPA_RUNNER_KEY and the given files."""
    from appa.runner import dom
    from appa.runner.browser import DEFAULT_PROFILE, Browser
    from appa.runner.client import RunnerClient
    from appa.runner.main import Runner, check_file

    config_mod.load_env()
    key = os.environ.get("APPA_RUNNER_KEY", "")
    if not key:
        raise SystemExit("set APPA_RUNNER_KEY (create one with `appa keys create <name> --scopes runner`)")
    files = dom.Files(
        resume=check_file(args.resume, "resume"), cover_letter=check_file(args.cover_letter, "cover letter")
    )
    if not files.resume:
        print("note: no --resume given; forms that need one will report that field as not filled")
    client = RunnerClient(args.server, key)
    profile = Path(args.profile_dir).expanduser() if args.profile_dir else DEFAULT_PROFILE
    with Browser(profile, headless=args.headless) as browser:
        Runner(client, browser, files).loop(once=args.once)


def cmd_eval(args) -> None:
    """`appa eval`: snapshot postings as eval cases, or score models on the labeled cases."""
    from appa import evals

    if args.action == "snapshot":
        for url in args.urls:
            path = evals.snapshot(job_from_url(url))
            print(f"saved {path.relative_to(config_mod.ROOT)}: fill in its expect: block, then run appa eval")
        return
    cases = evals.load_cases(only=args.case or None)
    if not cases:
        raise SystemExit("no labeled eval cases found")
    from appa import llm

    reports = []
    for model in args.model or [None]:
        try:
            rep = evals.run(model, cases, workers=args.workers, max_cost=args.max_cost)
        except llm.LLMError as e:
            raise SystemExit(f"eval: {e}") from e
        print(f"saved {evals.save(rep).relative_to(config_mod.ROOT)}")
        reports.append(rep)
    print()
    print(evals.format_reports(reports))


def cmd_migrate(args) -> None:
    """`appa migrate`: bring the database schema up to date."""
    from appa import migrate

    migrate.upgrade()  # runs as the owner role (APPA_MIGRATIONS_DATABASE_URL)
    print("database schema is up to date")


def cmd_serve(args) -> None:
    """`appa serve`: run the API; refuses non-loopback hosts unless APPA_BEHIND_PROXY is set."""
    import uvicorn

    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("APPA_BEHIND_PROXY"):
        raise SystemExit(
            "Refusing to bind to a non-loopback address. Run behind a reverse proxy and set "
            "APPA_BEHIND_PROXY=1 if you really mean it."
        )
    uvicorn.run(
        "appa.api.app:app",
        host=args.host,
        port=args.port,
        workers=1,
        proxy_headers=bool(os.environ.get("APPA_BEHIND_PROXY")),
        server_header=False,
        limit_concurrency=50,
        timeout_keep_alive=5,
    )


def cmd_init(_args) -> None:
    """`appa init`: copy the example profile to config/profile.yaml unless it exists."""
    dest = config_mod.DEFAULT_PATH
    if dest.exists():
        print(f"{dest} already exists; leaving it alone")
    else:
        shutil.copy(dest.parent / "profile.example.yaml", dest)
        print(f"created {dest}: fill in your details")


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and dispatch to the matching cmd_* function."""
    p = argparse.ArgumentParser(prog="appa", description="Job-search agents: find, screen and track applications.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create config/profile.yaml from the example")

    def run_opts(sp, queue_flag: bool = True):
        """Add the shared --model/--max-usd/--max-steps (and optionally --queue) options to a subcommand."""
        sp.add_argument("--model", help="screening model (default: config/models.yaml)")
        sp.add_argument("--max-usd", type=float, help="stop the run after spending this much")
        sp.add_argument("--max-steps", type=int, help="stop the run after this many tool calls")
        if queue_flag:
            sp.add_argument("--queue", action="store_true", help="hand to a worker instead of running here")

    a = sub.add_parser("add", help="screen one or more job links (a Scout run)")
    a.add_argument("urls", nargs="+")
    a.add_argument("--force", action="store_true", help="re-screen even if seen before")
    run_opts(a)
    s = sub.add_parser("screenshot", help="read job cards from screenshots, find and screen them")
    s.add_argument("images", nargs="+")
    s.add_argument("--screenshot-model", help="vision model that reads the screenshot")
    run_opts(s)
    po = sub.add_parser("poll", help="check company boards for new postings and screen them")
    po.add_argument("--board", action="append", help="greenhouse:<slug> etc. (default: all boards in config)")
    po.add_argument("--max-new", type=int, default=200)
    run_opts(po)
    sw = sub.add_parser("sweep", help="read every job source, poll company boards, screen the newest postings")
    sw.add_argument("--lists", help="comma-separated list sources (default: the policy's, usually all)")
    sw.add_argument("--boards", type=int, help="company boards to poll (default: policy feeds.boards_per_run)")
    sw.add_argument("--screen", type=int, help="inbox listings to screen (default: policy feeds.screen_per_run)")
    sw.add_argument("--no-screen", action="store_true", help="only collect into the inbox (no screening)")
    sw.add_argument("--max-new", type=int, default=500)
    run_opts(sw)
    sub.add_parser("feeds", help="job sources, what each needs, and inbox/board counts")
    ls = sub.add_parser("listings", help="the inbox of postings found; track/skip/restore by id")
    ls.add_argument("action", nargs="?", choices=["list", "track", "skip", "restore", "refilter"], default="list")
    ls.add_argument("ids", nargs="*", type=int)
    ls.add_argument("--status", choices=list(db.LISTING_STATUSES))
    ls.add_argument("--source", help="e.g. simplify_newgrad or board:workday")
    ls.add_argument("-q", help="company or title contains")
    ls.add_argument("--limit", type=int, default=40)
    bd = sub.add_parser("boards", help="company boards in rotation; seed / add <ats:slug> / disable <id>")
    bd.add_argument("action", nargs="?", choices=["list", "seed", "add", "disable", "enable"], default="list")
    bd.add_argument("specs", nargs="*")
    bd.add_argument("--ats")
    bd.add_argument("--limit", type=int, default=40)
    tk = sub.add_parser("takeover", help="apply to these yourself: stops appa's attempt, gives you the link")
    tk.add_argument("ids", nargs="+", type=int, help="application ids")
    r = sub.add_parser("run", help="start any agent role (queued for a worker unless --inline)")
    r.add_argument("role", choices=["scout", "screener", "concierge"])
    r.add_argument("--url", action="append")
    r.add_argument("--board", action="append")
    r.add_argument("--screenshot", action="append")
    r.add_argument("--input", help="role input as JSON")
    r.add_argument("--inline", action="store_true", help="run in this process instead of a worker")
    r.add_argument("--no-follow", action="store_true", help="don't stream events")
    run_opts(r, queue_flag=False)
    rs = sub.add_parser("runs", help="list recent runs, or show/follow one run's events")
    rs.add_argument("id", nargs="?", type=int)
    rs.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("cancel", help="cancel a run")
    c.add_argument("id", type=int)
    sub.add_parser("stop-all", help="kill switch: pause workers and cancel every active run")
    sub.add_parser("resume", help="undo stop-all")
    w = sub.add_parser("worker", help="execute queued runs (run one or more of these)")
    w.add_argument("--once", action="store_true", help="run at most one job, then exit")
    sub.add_parser("scheduler", help="queue runs for due schedules (run one of these)")
    sc = sub.add_parser("schedule", help="manage recurring runs")
    scs = sc.add_subparsers(dest="action", required=True)
    sca = scs.add_parser("add")
    sca.add_argument("role", choices=["scout", "screener", "concierge"])
    sca.add_argument("cron", help="5-field cron in UTC, e.g. '0 */4 * * *'")
    sca.add_argument("--board", action="append")
    sca.add_argument(
        "--sweep", action="store_true", help="scout only: every source, boards, then screen (see appa sweep)"
    )
    sca.add_argument("--input", help='JSON input for the role, e.g. \'{"sweep": true, "screen_inbox": 600}\'')
    run_opts(sca, queue_flag=False)
    scs.add_parser("list")
    for name in ("rm", "on", "off"):
        x = scs.add_parser(name)
        x.add_argument("id", type=int)
    sub.add_parser("questions", help="list open questions for you")
    lst = sub.add_parser("list", help="show recently evaluated postings")
    lst.add_argument("--verdict", choices=[v.value for v in Verdict])
    lst.add_argument("--limit", type=int, default=50)
    b = sub.add_parser("backfill", help="import your tracker CSV into the database (safe to re-run)")
    b.add_argument("csv")
    b.add_argument("--email", help="applied_with_email for imported rows (default: candidate.application_email)")
    e = sub.add_parser("export", help="write all applications to a CSV (e.g. to paste into Google Sheets)")
    e.add_argument("csv")
    k = sub.add_parser("keys", help="manage API keys for your agents")
    ksub = k.add_subparsers(dest="action", required=True)
    kc = ksub.add_parser("create")
    kc.add_argument("name")
    kc.add_argument(
        "--scopes",
        default="applications:read,applications:write",
        help="comma-separated: applications:read, applications:write, runs:read, runs:write, runner",
    )
    ksub.add_parser("list")
    kr = ksub.add_parser("revoke")
    kr.add_argument("name")
    sub.add_parser("migrate", help="apply database migrations (run after every upgrade)")
    ids = sub.add_parser("ids", help="give rows without a job ID one from their own notes (preview; --apply)")
    ids.add_argument("--apply", action="store_true")
    tr = sub.add_parser("track", help="add postings you picked as To Apply (no screening)")
    tr.add_argument("urls", nargs="+")
    tr.add_argument("--job-id", help="the posting's ID, when the link doesn't carry one")
    ap = sub.add_parser("apply", help="fill applications in your browser (each waits for your approval)")
    ap.add_argument("ids", nargs="*", type=int, help="application ids")
    ap.add_argument("--all-to-apply", action="store_true", help="every application with status To Apply")
    ap.add_argument(
        "--redo", type=int, metavar="ATTEMPT", help="fill an attempt again with your current answers (before approval)"
    )
    at = sub.add_parser("attempts", help="recent application attempts")
    at.add_argument("--limit", type=int, default=20)
    ins = sub.add_parser("insights", help="most common misses and failures: what to improve next")
    ins.add_argument("--days", type=int, default=30)
    ins.add_argument("--kind", help="one kind only, e.g. label_unmapped, id_not_in_link, fill_failed")
    ins.add_argument("--limit", type=int, default=40)
    ins.add_argument("--json", action="store_true", help="machine-readable, for an agent to work through")
    ins.add_argument("--backfill", action="store_true", help="once: seed from postings and attempts so far")
    ins.add_argument("--prune", type=int, metavar="DAYS", help="delete observations older than DAYS")
    apr = sub.add_parser("approve", help="approve a filled application for submission")
    apr.add_argument("attempt_id", type=int)
    an = sub.add_parser("answers", help="your answers bank for application forms")
    ans = an.add_subparsers(dest="action", required=True)
    ans.add_parser("list")
    x = ans.add_parser("set")
    x.add_argument("key")
    x.add_argument("value")
    x = ans.add_parser("rm")
    x.add_argument("key")
    x = ans.add_parser("import", help="load a YAML file (see config/answers.example.yaml)")
    x.add_argument("file")
    rn = sub.add_parser("runner", help="run the browser runner on this machine")
    rn.add_argument("--server", default="http://127.0.0.1:8787", help="your appa server (https:// unless localhost)")
    rn.add_argument("--resume", help="PDF/Word résumé to attach")
    rn.add_argument("--cover-letter", help="PDF/Word cover letter to attach when asked")
    rn.add_argument("--profile-dir", help="Chrome profile directory (default: a dedicated appa profile)")
    rn.add_argument("--headless", action="store_true", help="hide the browser (testing only)")
    rn.add_argument("--once", action="store_true", help="exit when there's nothing to do")
    us = sub.add_parser("user", help="manage your web UI login")
    uss = us.add_subparsers(dest="action", required=True)
    for name, helptext in (
        ("create", "create a login"),
        ("passwd", "change password (logs out everywhere)"),
        ("totp", "turn on two-factor codes"),
        ("unlock", "clear a lockout"),
    ):
        x = uss.add_parser(name, help=helptext)
        x.add_argument("username")
    ev = sub.add_parser("eval", help="score screening models on labeled postings (accuracy + cost)")
    ev.add_argument("action", nargs="?", choices=["run", "snapshot"], default="run")
    ev.add_argument("urls", nargs="*", help="with snapshot: postings to save for labeling")
    ev.add_argument("--model", action="append", help="model to score; repeat to compare (default: screener default)")
    ev.add_argument("--case", action="append", help="only run these case ids")
    ev.add_argument("--workers", type=int, default=4)
    ev.add_argument("--max-cost", type=float, default=1.0, help="stop a model's run after this many USD")
    sv = sub.add_parser("serve", help="run the HTTP API (localhost only by default)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    args = p.parse_args(argv)

    commands = {
        "init": cmd_init,
        "backfill": cmd_backfill,
        "export": cmd_export,
        "keys": cmd_keys,
        "serve": cmd_serve,
        "migrate": cmd_migrate,
        "eval": cmd_eval,
        "add": cmd_add,
        "screenshot": cmd_screenshot,
        "poll": cmd_poll,
        "run": cmd_run,
        "runs": cmd_runs,
        "cancel": cmd_cancel,
        "stop-all": cmd_stop_all,
        "resume": cmd_resume,
        "worker": cmd_worker,
        "scheduler": cmd_scheduler,
        "schedule": cmd_schedule,
        "questions": cmd_questions,
        "list": cmd_list,
        "user": cmd_user,
        "ids": cmd_ids,
        "track": cmd_track,
        "apply": cmd_apply,
        "attempts": cmd_attempts,
        "insights": cmd_insights,
        "sweep": cmd_sweep,
        "feeds": cmd_feeds,
        "listings": cmd_listings,
        "boards": cmd_boards,
        "takeover": cmd_takeover,
        "approve": cmd_approve,
        "answers": cmd_answers,
        "runner": cmd_runner,
    }
    commands[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
