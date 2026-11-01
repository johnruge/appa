"""Prefilling applications, end to end. The runner (your browser) and the API both go through here.

    start ──► [runner: open form, report questions] ──► plan
                 │ missing answers → questions for you → (you answer) → prepare again
                 └ all answered → [runner: fill, screenshot, leave the tab open] → ready
                                    → (you check it and press submit yourself) → you mark it applied

appa never submits an application. It fills the form in your browser and hands it to you; the
only way an application becomes Applied is you saying so.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from appa import db, observe
from appa.apply.planner import Plan, build_plan
from appa.core.apply_fields import FormQuestion, saved_key, standard_key
from appa.core.ats_urls import parse_url
from appa.runtime import queue

ACTIVE = ("preparing", "needs_you", "ready")  # an attempt in one of these blocks starting another
RUNNER_EVENT_KINDS = {"log", "tool_call"}


class ApplyError(ValueError):
    pass


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


# ---- where to apply ------------------------------------------------------------------

# Sites that require an account or login to apply (or forbid automation). appa never creates
# accounts or signs in for you: these come back to you as "apply yourself".
LOGIN_SITES = {
    "myworkdayjobs.com": "Workday needs an account",
    "myworkdaysite.com": "Workday needs an account",
    "icims.com": "iCIMS usually needs an account",
    "taleo.net": "Taleo needs an account",
    "successfactors.com": "SuccessFactors needs an account",
    "successfactors.eu": "SuccessFactors needs an account",
    "oraclecloud.com": "Oracle Cloud needs an account",
    "brassring.com": "BrassRing needs an account",
    "linkedin.com": "LinkedIn doesn't allow automated applying",
    "indeed.com": "Indeed needs an account",
    "handshake.com": "Handshake needs your login",
    "joinhandshake.com": "Handshake needs your login",
}


def login_site(host: str) -> str | None:
    """Why this host needs you to log in yourself, or None if it is not a known login site."""
    host = host.lower()
    return next((why for d, why in LOGIN_SITES.items() if host == d or host.endswith("." + d)), None)


def apply_url(link: str | None) -> tuple[str, str]:
    """(ats, url to open). Greenhouse/Lever/Ashby go straight to their form; any other public
    job page is opened as-is and the runner finds the form (or hands it back to you)."""
    from urllib.parse import urlparse

    ref = parse_url(link or "")
    if ref and ref.job_id and ref.slug:
        if ref.ats == "greenhouse":
            return "greenhouse", f"https://job-boards.greenhouse.io/{ref.slug}/jobs/{ref.job_id}"
        if ref.ats == "lever":
            return "lever", f"https://jobs.lever.co/{ref.slug}/{ref.job_id}/apply"
        return "ashby", f"https://jobs.ashbyhq.com/{ref.slug}/{ref.job_id}/application"
    u = urlparse((link or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ApplyError("no job link on this application; apply yourself")
    if why := login_site(u.hostname):
        raise ApplyError(f"{why}; apply yourself")
    if re.search(r"/(jobTasks/completed|candidate/login|applications/dashboard|my-profile|userHome)\b", u.path):
        raise ApplyError("this link is a confirmation or dashboard page, not the posting; apply yourself")
    return "generic", u.geturl()


# ---- answers bank ---------------------------------------------------------------------


def answers(s: Session) -> dict[str, str]:
    """The whole answer bank as key -> value."""
    return {a.key: a.value for a in s.scalars(select(db.Answer))}


def set_answer(s: Session, key: str, value: str, source: str = "you") -> None:
    """Insert or update one saved answer and commit."""
    row = s.get(db.Answer, key)
    if row:
        row.value, row.source = value, source
    else:
        s.add(db.Answer(key=key, value=value, source=source))
    s.commit()


# ---- lifecycle ------------------------------------------------------------------------


def _run(s: Session, attempt: db.ApplyAttempt) -> db.Run | None:
    """The run behind an attempt, if any."""
    return s.get(db.Run, attempt.run_id) if attempt.run_id else None


def _event(s: Session, attempt: db.ApplyAttempt, kind: str, /, **payload) -> None:
    """Add an event to the attempt's run log (the caller commits)."""
    if attempt.run_id:
        s.add(db.RunEvent(run_id=attempt.run_id, kind=kind, payload=payload))


def _set_run(s: Session, attempt: db.ApplyAttempt, status: str, error: str | None = None) -> None:
    """Set the attempt's run status, stamping start/finish times, and log a status event (the caller commits)."""
    run = _run(s, attempt)
    if not run:
        return
    run.status = status
    if status == "running" and not run.started_at:
        run.started_at = _now()
    if status in db.TERMINAL:
        run.finished_at, run.error = _now(), error
    _event(
        s,
        attempt,
        "status",
        status=status,
        error=error,
        cost_usd=round(run.cost_usd or 0, 6),
        steps=run.steps,
        model_calls=run.model_calls,
    )


def start(s: Session, application_id: int, by: str) -> db.ApplyAttempt:
    """Create an applier run and attempt for an application and queue its prepare step; commits."""
    from appa.runtime import runs

    app = s.get(db.Application, application_id)
    if not app:
        raise ApplyError(f"no application #{application_id}")
    if app.status in db.APPLIED_STATUSES:
        raise ApplyError(f"#{application_id} is already {app.status}")
    if s.scalars(
        select(db.ApplyAttempt).where(db.ApplyAttempt.application_id == app.id, db.ApplyAttempt.status.in_(ACTIVE))
    ).first():
        raise ApplyError(f"#{application_id} already has an application in progress")
    ats, url = apply_url(app.link)
    run = runs.create(s, "applier", input={"application_id": app.id}, created_by=by, enqueue=False)
    attempt = db.ApplyAttempt(application_id=app.id, run_id=run.id, ats=ats, apply_url=url, created_by=by)
    s.add(attempt)
    s.flush()
    queue.enqueue(s, "apply", run.id, max_attempts=2)
    _last_job(s, run.id).payload = {"attempt_id": attempt.id, "phase": "prepare"}
    s.add(
        db.RunEvent(
            run_id=run.id, kind="log", payload={"message": f"apply to {app.company} / {app.role} via {ats}: {url}"}
        )
    )
    s.commit()
    return attempt


def _last_job(s: Session, run_id: int) -> db.QueueJob:
    """The most recent queue job for a run."""
    return s.scalars(select(db.QueueJob).where(db.QueueJob.run_id == run_id).order_by(db.QueueJob.id.desc())).first()


def _enqueue(s: Session, attempt: db.ApplyAttempt, phase: str) -> None:
    """Queue the attempt's prepare step for the runner."""
    queue.enqueue(s, "apply", attempt.run_id, max_attempts=2)
    _last_job(s, attempt.run_id).payload = {"attempt_id": attempt.id, "phase": phase}


def claim(s: Session, runner: str) -> dict | None:
    """Lease the next apply job to a runner, skipping stale ones; returns what the runner needs, or None."""
    job = queue.claim(s, runner, kinds=("apply",))
    if not job:
        return None
    attempt = s.get(db.ApplyAttempt, job.payload.get("attempt_id"), with_for_update=True)
    phase = job.payload.get("phase")
    if not attempt or attempt.status != "preparing" or phase != "prepare":
        queue.done(s, job)  # stale (cancelled, already moved on, or an old submit job: appa never submits)
        return claim(s, runner)
    _set_run(s, attempt, "running")
    _event(s, attempt, "log", message=f"runner {runner} picked up the {phase} step")
    s.commit()
    return {
        "job_id": job.id,
        "attempt_id": attempt.id,
        "phase": phase,
        "ats": attempt.ats,
        "apply_url": attempt.apply_url,
    }


def record_events(s: Session, attempt_id: int, events: list[dict]) -> None:
    """Append up to 200 runner events of allowed kinds to the run log, truncated; commits."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    for ev in events[:200]:
        kind = ev.get("kind")
        if kind in RUNNER_EVENT_KINDS and isinstance(ev.get("payload"), dict):
            _event(s, attempt, kind, **{k: str(v)[:500] for k, v in list(ev["payload"].items())[:12]})
    s.commit()


def receive_form(s: Session, attempt_id: int, questions: list[FormQuestion], choose=None) -> dict:
    """The runner found these questions. Reply with a plan to fill, or stop and ask you."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    if attempt.status != "preparing":
        raise ApplyError(f"attempt is {attempt.status}, not preparing")
    attempt.form = [q.model_dump() for q in questions]
    plan: Plan = build_plan(questions, answers(s), choose)
    attempt.plan = [i.model_dump() for i in plan.items]
    filled = sum(1 for i in plan.items if i.value is not None)
    _event(
        s,
        attempt,
        "log",
        message=f"form has {len(questions)} questions: {filled} answered from your bank, {len(plan.missing)} need you",
    )
    _observe_form(s, attempt, questions, plan)
    if plan.ready:
        s.commit()
        return {"action": "fill", "plan": attempt.plan}
    asked = {
        q.context.get("key")
        for q in s.scalars(select(db.Question).where(db.Question.status == "open", db.Question.kind == "apply_field"))
    }
    app = s.get(db.Application, attempt.application_id)
    for m in plan.missing:
        key = saved_key(m.question.label)
        if key in asked:
            continue  # same question already waiting from another application: one answer serves both
        q = db.Question(
            run_id=attempt.run_id,
            application_id=attempt.application_id,
            kind="apply_field",
            prompt=f"{app.company}: {m.question.label}"[:4000],
            context={
                "attempt_id": attempt.id,
                "key": key,
                "label": m.question.label,
                "kind": m.question.kind,
                "options": m.question.options[:200],
                "reason": m.reason,
                "standard_key": standard_key(m.question),
            },
        )
        s.add(q)
        s.flush()
        asked.add(key)
        _event(
            s, attempt, "question", question_id=q.id, prompt=m.question.label[:300], kind="apply_field", reason=m.reason
        )
    attempt.status = "needs_you"
    _set_run(s, attempt, "waiting_for_you")
    s.commit()
    return {"action": "stop", "reason": f"{len(plan.missing)} question(s) need your answer"}


def _observe_form(s: Session, attempt: db.ApplyAttempt, questions: list[FormQuestion], plan: Plan) -> None:
    """Record questions no standard key covers, and answers only the model could map."""
    by_ref = {q.ref: q for q in questions}
    for i in plan.items:
        if i.source.startswith("model:"):
            observe.record(
                s,
                "label_by_model",
                i.label,
                url=attempt.apply_url,
                attempt_id=attempt.id,
                application_id=attempt.application_id,
                detail={"key": i.source[6:], "kind": i.kind},
            )
    for m in plan.missing:
        q = m.question
        if q.kind != "textarea" and standard_key(q) is None and q.ref in by_ref:
            observe.record(
                s,
                "label_unmapped",
                q.label,
                url=attempt.apply_url,
                attempt_id=attempt.id,
                application_id=attempt.application_id,
                detail={"kind": q.kind, "reason": m.reason, "options": "; ".join(q.options[:8])},
            )


def receive_preview(s: Session, attempt_id: int, report: list[dict], screenshot_upload_id: int | None) -> None:
    """Store the fill report and preview, log failed fields, and tell you the filled form is waiting in your
    browser for you to check and submit; commits."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    if attempt.status != "preparing":
        raise ApplyError(f"attempt is {attempt.status}, not preparing")
    attempt.fill_report = report[:500]
    attempt.preview_upload_id = screenshot_upload_id
    required = {q["ref"] for q in attempt.form or [] if q.get("required")}
    failed = [r for r in report if not r.get("ok")]
    failed_required = [r for r in failed if r.get("ref") in required]
    app = s.get(db.Application, attempt.application_id)
    kinds = {q["ref"]: q.get("kind") for q in attempt.form or []}
    for r in failed[:50]:
        observe.record(
            s,
            "fill_failed",
            r.get("label") or r.get("ref") or "?",
            url=attempt.apply_url,
            attempt_id=attempt.id,
            application_id=app.id,
            detail={"kind": kinds.get(r.get("ref")), "error": r.get("error"), "ref": r.get("ref")},
        )
    attempt.status = "ready"
    note = ""
    if failed:
        note = f" {len(failed)} field(s) didn't fill" + (", including required ones" if failed_required else "") + "."
    s.add(
        db.Question(
            run_id=attempt.run_id,
            application_id=attempt.application_id,
            kind="apply_ready",
            prompt=(
                f"{app.company} / {app.role} is filled in your browser. Check it, submit it there yourself, "
                f"then mark it applied.{note}"
            )[:4000],
            context={
                "attempt_id": attempt.id,
                "preview_upload_id": screenshot_upload_id,
                "apply_url": attempt.apply_url,
                "failed": [{"ref": r.get("ref"), "label": r.get("label"), "error": r.get("error")} for r in failed][
                    :50
                ],
                "required_unfilled": bool(failed_required),
                "plan": [
                    {"label": i["label"][:200], "value": i["value"], "source": i["source"]}
                    for i in attempt.plan or []
                    if i["value"] is not None
                ][:100],
            },
        )
    )
    _event(s, attempt, "log", message="form filled and left open in your browser for you to submit." + note)
    _set_run(s, attempt, "waiting_for_you")
    s.commit()


def mark_applied(s: Session, application_id: int, by: str) -> db.Application:
    """You submitted it: mark the application Applied (today) and close its prefill attempt and questions. The only
    way anything becomes Applied. Commits."""
    app = s.get(db.Application, application_id)
    if not app:
        raise ApplyError(f"no application #{application_id}")
    for attempt in s.scalars(
        select(db.ApplyAttempt).where(db.ApplyAttempt.application_id == app.id, db.ApplyAttempt.status.in_(ACTIVE))
    ):
        if attempt.status == "preparing":
            for job in s.scalars(
                select(db.QueueJob).where(db.QueueJob.run_id == attempt.run_id, db.QueueJob.status == "queued")
            ):
                job.status = "done"
        attempt.status, attempt.submitted_at = "applied", _now()
        _close_questions(s, attempt.id, None, "applied")
        _event(s, attempt, "log", message=f"{by} submitted it and marked it applied")
        _set_run(s, attempt, "succeeded")
    app.status = "Applied"
    app.date_applied = app.date_applied or date.today()
    app.applied_with_email = app.applied_with_email or answers(s).get("email")
    s.commit()
    return app


def fail(s: Session, job_id: int, error: str, *, retry: bool) -> None:
    """Fail a queue job; once it has no retries left, mark its attempt and run failed."""
    job = s.get(db.QueueJob, job_id)
    if not job:
        return
    queue.fail(s, job, error, retry=retry)
    attempt = s.get(db.ApplyAttempt, job.payload.get("attempt_id"))
    if attempt and job.status == "failed" and attempt.status in ACTIVE:
        attempt.status, attempt.error = "failed", error[:2000]
        observe.record(
            s,
            "runner_error",
            error[:200],
            url=attempt.apply_url,
            attempt_id=attempt.id,
            application_id=attempt.application_id,
            detail={"phase": job.payload.get("phase")},
        )
        _set_run(s, attempt, "failed", error[:500])
        s.commit()


def blocked(s: Session, attempt_id: int, reason: str) -> db.ApplyAttempt:
    """The runner can't fill this one (login, multi-page form, no form found): it's yours."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    if attempt.status != "preparing":
        raise ApplyError(f"attempt is {attempt.status}")
    app = s.get(db.Application, attempt.application_id)
    attempt.status, attempt.error = "failed", f"apply yourself: {reason}"[:2000]
    app.status = "Apply Myself"
    observe.record(s, "blocked", reason[:200], url=attempt.apply_url, attempt_id=attempt.id, application_id=app.id)
    note = f"appa couldn't fill it: {reason}"
    app.notes = f"{app.notes}; {note}" if app.notes else note
    s.add(
        db.Question(
            run_id=attempt.run_id,
            application_id=app.id,
            kind="review",
            prompt=f"Apply to {app.company} / {app.role} yourself: {reason}.",
            context={"link": attempt.apply_url, "attempt_id": attempt.id},
        )
    )
    _event(s, attempt, "log", message=f"handed back to you: {reason}")
    _set_run(s, attempt, "failed", f"apply yourself: {reason}"[:500])
    s.commit()
    return attempt


def redo(s: Session, attempt_id: int, by: str) -> db.ApplyAttempt:
    """Fill the form again from your current answers bank (e.g. after you added answers).
    Replaces the pending card. Never submits."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    if not attempt or attempt.status not in ("ready", "needs_you", "failed"):
        raise ApplyError("redo works on attempts that are ready, waiting for your answers, or failed")
    _close_questions(s, attempt.id, None, "dismissed")
    attempt.status, attempt.error, attempt.fill_report, attempt.preview_upload_id = "preparing", None, None, None
    _event(s, attempt, "log", message=f"{by} asked to fill it again with the current answers")
    _set_run(s, attempt, "queued")
    _enqueue(s, attempt, "prepare")
    s.commit()
    return attempt


def cancel(s: Session, attempt_id: int) -> db.ApplyAttempt:
    """Cancel an active attempt, its queued jobs and open questions; commits. Returns the attempt either way."""
    attempt = s.get(db.ApplyAttempt, attempt_id)
    if not attempt:
        raise ApplyError("not found")
    if attempt.status in ACTIVE:
        attempt.status = "cancelled"
        for job in s.scalars(
            select(db.QueueJob).where(db.QueueJob.run_id == attempt.run_id, db.QueueJob.status == "queued")
        ):
            job.status = "done"
        _close_questions(s, attempt.id, None, "dismissed")
        _set_run(s, attempt, "cancelled", "cancelled")
        s.commit()
    return attempt


def _close_questions(s: Session, attempt_id: int, kind: str | None, answer: str) -> None:
    """Close an attempt's open questions (optionally one kind) with the given answer (the caller commits)."""
    for q in s.scalars(select(db.Question).where(db.Question.status == "open")):
        if q.context.get("attempt_id") == attempt_id and (kind is None or q.kind == kind):
            q.status, q.answer, q.answered_at = ("dismissed" if answer == "dismissed" else "answered"), answer, _now()


def answer_field(s: Session, question: db.Question, value: str, by: str) -> list[int]:
    """Save your answer for this question (reused on every future form that asks it), then
    re-prepare every attempt that was only waiting on answers. Returns those attempt ids."""
    value = value.strip()
    if not value:
        raise ApplyError("empty answer")
    opts = question.context.get("options") or []
    if opts and question.context.get("kind") not in ("checkbox_group",):
        from appa.core.apply_fields import match_option

        if not match_option(value, opts):
            raise ApplyError(f"answer must be one of: {', '.join(opts[:20])}")
    set_answer(s, question.context["key"], value, source=by)
    std = question.context.get("standard_key")
    if std and not s.get(db.Answer, std) and question.context.get("kind") != "textarea":
        set_answer(s, std, value, source=by)  # differently worded forms reuse it too
    question.status, question.answer, question.answered_at = "answered", value, _now()
    resumed = []
    waiting = s.scalars(select(db.ApplyAttempt).where(db.ApplyAttempt.status == "needs_you")).all()
    open_keys = {
        q.context.get("attempt_id")
        for q in s.scalars(select(db.Question).where(db.Question.status == "open", db.Question.kind == "apply_field"))
    }
    for attempt in waiting:
        if attempt.id in open_keys:
            continue
        attempt.status = "preparing"
        _set_run(s, attempt, "queued")
        _enqueue(s, attempt, "prepare")
        resumed.append(attempt.id)
    s.commit()
    return resumed


def takeover(s: Session, application_id: int, by: str) -> tuple[db.Application, str | None]:
    """You'll apply to this one yourself, from scratch: stop any prefill in progress, close its questions and
    mark it Apply Myself. Returns the application and the link to open. Commits."""
    app = s.get(db.Application, application_id)
    if not app:
        raise ApplyError(f"no application #{application_id}")
    if app.status in db.APPLIED_STATUSES:
        raise ApplyError(f"#{application_id} is already {app.status}")
    link = app.link
    attempt = s.scalars(
        select(db.ApplyAttempt)
        .where(db.ApplyAttempt.application_id == app.id, db.ApplyAttempt.status.in_(ACTIVE))
        .with_for_update()
    ).first()
    if attempt:
        link = attempt.apply_url
        for job in s.scalars(
            select(db.QueueJob).where(db.QueueJob.run_id == attempt.run_id, db.QueueJob.status == "queued")
        ):
            job.status = "done"
        _close_questions(s, attempt.id, None, "dismissed")
        attempt.status, attempt.error = "cancelled", f"{by} took over"
        _event(s, attempt, "log", message=f"{by} took over: applying themselves")
        _set_run(s, attempt, "cancelled", "you took over")
    app.status = "Apply Myself"
    note = "you took over from appa"
    app.notes = f"{app.notes}; {note}" if app.notes else note
    s.commit()
    return app, link
