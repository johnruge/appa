"""Runtime: queue, runs, budgets, cancel, kill switch, worker, scheduler, and roles.
Postgres-backed; models and network are faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from appa import db, llm
from appa.agents import base as agents_base
from appa.config import Candidate, Companies, Config, Rules
from appa.core.models import Extraction, Job
from appa.runtime import queue, runs, scheduler
from appa.runtime.context import is_paused, set_paused
from appa.runtime.worker import Worker

# ---- a test role --------------------------------------------------------------------


class EchoInput(BaseModel):
    steps: int = 1
    spend: float = 0.0  # charged per step
    fail: bool = False


def echo_run(ctx, p: EchoInput) -> dict:
    ctx.log("starting")
    for i in range(p.steps):
        ctx.tool("echo", lambda x: x * 2, i, summarize=str)
        if p.spend:
            ctx.charge(llm.Completion(model="test/cheap", cost_usd=p.spend), purpose="think")
    if p.fail:
        raise RuntimeError("boom")
    return {"counts": {"done": p.steps}}


ECHO = agents_base.RoleSpec(
    "echo", "test role", EchoInput, echo_run, default_budget={"max_usd": 1.0, "max_steps": 100, "max_model_calls": 100}
)


@pytest.fixture()
def roles(monkeypatch):
    import appa.agents as agents

    real = agents._roles
    monkeypatch.setattr(agents, "_roles", lambda: {**real(), "echo": ECHO})


def events(factory, run_id):
    with factory() as s:
        return list(s.query(db.RunEvent).filter_by(run_id=run_id).order_by(db.RunEvent.id))


# ---- queue --------------------------------------------------------------------------


def test_claim_is_exclusive_and_leases_expire(factory, roles):
    with factory() as s:
        run = runs.create(s, "echo")
    with factory() as a, factory() as b:
        ja = queue.claim(a, "worker-a")
        assert ja is not None and ja.run_id == run.id
        assert queue.claim(b, "worker-b") is None  # locked / leased
        ja.lease_until = datetime.now(UTC) - timedelta(seconds=1)  # worker-a "died"
        a.commit()
        jb = queue.claim(b, "worker-b")
        assert jb.id == ja.id and jb.attempts == 2 and jb.leased_by == "worker-b"


def test_fail_retries_with_backoff_then_gives_up(factory, roles):
    with factory() as s:
        runs.create(s, "echo")
        job = queue.claim(s, "w")
        queue.fail(s, job, "db hiccup", retry=True)
        assert job.status == "queued" and job.available_at > datetime.now(UTC)
        job.attempts = job.max_attempts
        queue.fail(s, job, "still broken", retry=True)
        assert job.status == "failed"


# ---- runs ---------------------------------------------------------------------------


def test_create_validates_input_and_model(factory, roles):
    with factory() as s:
        with pytest.raises(ValidationError):
            runs.create(s, "echo", input={"steps": "lots"})
        with pytest.raises(llm.ModelNotAllowed):
            runs.create(s, "echo", model="openai/not-allowed")
        with pytest.raises(ValueError, match="unknown role"):
            runs.create(s, "nope")


def test_execute_records_everything(factory, roles):
    with factory() as s:
        run = runs.create(s, "echo", input={"steps": 3, "spend": 0.01}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "succeeded" and done.result == {"counts": {"done": 3}}
    assert done.steps == 3 and done.model_calls == 3 and done.cost_usd == pytest.approx(0.03)
    kinds = [e.kind for e in events(factory, run.id)]
    assert kinds[0] == "status" and kinds[-1] == "status"
    assert kinds.count("tool_call") == 3 and kinds.count("model_call") == 3 and "result" in kinds


def test_role_crash_is_recorded_not_raised(factory, roles):
    with factory() as s:
        run = runs.create(s, "echo", input={"fail": True}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "failed" and "boom" in done.error


def test_budget_stops_the_run(factory, roles):
    with factory() as s:
        run = runs.create(s, "echo", input={"steps": 10, "spend": 0.3}, budget={"max_usd": 0.5}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "cancelled" and "budget" in done.error
    assert done.steps == 2 and done.cost_usd == pytest.approx(0.6)  # stops at the next check


def test_step_budget(factory, roles):
    with factory() as s:
        run = runs.create(s, "echo", input={"steps": 10}, budget={"max_steps": 4}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "cancelled" and done.steps == 4


def test_cancel_before_and_during(factory, roles, monkeypatch):
    with factory() as s:
        queued = runs.create(s, "echo")
        assert runs.cancel(s, queued.id).status == "cancelled"
        running = runs.create(s, "echo", input={"steps": 50}, enqueue=False)

    def slow_echo(ctx, p):
        for i in range(p.steps):
            if i == 3:
                with factory() as s2:
                    s2.get(db.Run, ctx.run_id).cancel_requested = True
                    s2.commit()
                ctx._last_flag_check = 0  # don't wait for the 1s flag-check interval
            ctx.tool("echo", lambda x: x, i)
        return {}

    import appa.agents as agents

    monkeypatch.setattr(agents, "_roles", lambda: {"echo": agents_base.RoleSpec("echo", "", EchoInput, slow_echo)})
    done = runs.execute(factory, running.id)
    assert done.status == "cancelled" and done.error == "cancelled" and done.steps == 3


def test_daily_cap(factory, roles):
    with factory() as s:
        old = runs.create(s, "echo", enqueue=False)
        s.get(db.Run, old.id).cost_usd = 5.0
        s.commit()
        run = runs.create(s, "echo", input={"steps": 2}, enqueue=False)
    done = runs.execute(factory, run.id, daily_cap_usd=5.0)
    assert done.status == "cancelled" and "daily spend cap" in done.error


def test_stop_all_and_worker(factory, roles):
    with factory() as s:
        a = runs.create(s, "echo")
        b = runs.create(s, "echo")
        assert runs.stop_all(s) == 2 and is_paused(s)
        assert s.get(db.Run, a.id).status == "cancelled"
    w = Worker(factory, log=lambda *_: None)
    with factory() as s:
        c = runs.create(s, "echo", input={"steps": 2})
    assert w.run_one() is False  # paused: nothing claimed
    with factory() as s:
        set_paused(s, False)
    assert w.run_one() is True
    with factory() as s:
        assert s.get(db.Run, c.id).status == "succeeded"
        assert s.query(db.QueueJob).filter_by(run_id=c.id).one().status == "done"
        assert s.get(db.Run, b.id).status == "cancelled"


# ---- scheduler ----------------------------------------------------------------------


def test_scheduler_fires_due_schedules_once(factory, roles):
    with factory() as s:
        sch = scheduler.add(s, "echo", "0 */4 * * *", input={"steps": 1})
        first = sch.next_run_at
        assert scheduler.tick(s, now=first - timedelta(seconds=1)) == []
        created = scheduler.tick(s, now=first + timedelta(seconds=1))
        assert len(created) == 1
        assert s.get(db.Run, created[0]).created_by == f"schedule:{sch.id}"
        assert s.get(db.Schedule, sch.id).next_run_at == first + timedelta(hours=4)
        assert scheduler.tick(s, now=first + timedelta(seconds=2)) == []
        with pytest.raises(ValueError, match="invalid cron"):
            scheduler.add(s, "echo", "every tuesday")


# ---- roles end to end ---------------------------------------------------------------


def _ex(**kw) -> Extraction:
    base = dict(
        company="Hooli",
        title="Software Engineer, New Grad",
        location="NYC",
        is_staffing_agency=False,
        sponsorship="not_mentioned",
        sponsorship_quote="",
        citizenship_required=False,
        citizenship_quote="",
        clearance_required=False,
        clearance_quote="",
        min_years_experience=None,
        experience_quote="",
        is_entry_level=True,
        seniority="new_grad",
        has_essay_questions=False,
        suspicious_instructions=[],
    )
    return Extraction(**{**base, **kw})


@pytest.fixture()
def scout_env(monkeypatch):
    cfg = Config(
        candidate=Candidate(name="t", application_email="t@x.edu"),
        rules=Rules(title_includes=["engineer"], manual_companies=["Globex"], never_apply=["Initech"]),
        companies=Companies(greenhouse=["hooli"]),
    )
    monkeypatch.setattr("appa.config.load", lambda path=None: cfg)
    monkeypatch.setattr("appa.agents.screener.has_model_key", lambda: True)  # these tests fake the model
    jobs = [
        Job(
            company="Hooli",
            title="Software Engineer, New Grad",
            url="https://job-boards.greenhouse.io/hooli/jobs/1",
            description="Build things with Python. " * 20,
            ats="greenhouse",
            ats_job_id="1",
        ),
        Job(
            company="Hooli",
            title="Account Executive",
            url="https://job-boards.greenhouse.io/hooli/jobs/2",
            description="Sell things. " * 30,
            ats="greenhouse",
            ats_job_id="2",
        ),
        Job(
            company="Globex",
            title="Backend Engineer",
            url="https://job-boards.greenhouse.io/globex/jobs/3",
            description="Build things with Go. " * 20,
            ats="greenhouse",
            ats_job_id="3",
        ),
        Job(
            company="Initech",
            title="Software Engineer",
            url="https://job-boards.greenhouse.io/initech/jobs/4",
            description="Build things with Java. " * 20,
            ats="greenhouse",
            ats_job_id="4",
        ),
    ]
    monkeypatch.setattr("appa.sources.ats.fetch_board", lambda kind, slug: [j.model_copy() for j in jobs])
    monkeypatch.setattr(
        "appa.extract.extract_with_meta",
        lambda job, model=None: (
            _ex(company=job.company, title=job.title),
            llm.Completion(model="test", cost_usd=0.001),
        ),
    )
    monkeypatch.setattr("appa.agents.screener.load_profile", lambda: None)
    return cfg


def test_scout_poll_end_to_end(factory, scout_env):
    with factory() as s:
        run = runs.create(s, "scout", input={"boards": ["config"]}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "succeeded", done.error
    counts = done.result["counts"]
    assert counts == {"candidate": 1, "reject": 2, "manual": 1}  # AE title, never-apply Initech
    assert done.model_calls == 3 and done.cost_usd == pytest.approx(0.003)  # no model call for the AE title
    with factory() as s:
        statuses = {a.company: a.status for a in s.query(db.Application)}
        assert statuses == {"Hooli": "To Apply", "Globex": "Apply Myself"}
        assert s.query(db.SeenJob).filter_by(run_id=run.id).count() == 4
    # Second poll: everything already seen, so no model calls at all
    with factory() as s:
        again = runs.create(s, "scout", input={"boards": ["greenhouse:hooli"]}, enqueue=False)
    done2 = runs.execute(factory, again.id)
    assert done2.model_calls == 0 and done2.result["counts"] == {"already_seen": 4}


def test_profile_routing_sees_only_company_title_location(factory, scout_env, monkeypatch):
    monkeypatch.setattr("appa.agents.screener.load_profile", lambda: "## Apply myself\n- Hooli")
    seen_prompts = []

    def fake_complete(role, messages, *, schema=None, model=None, vision=False, max_output_tokens=None):
        seen_prompts.append(messages[-1]["content"])
        return llm.Completion(
            model="test", cost_usd=0.0005, parsed=schema(route="manual", reason="Hooli is on the apply-myself list")
        )

    monkeypatch.setattr("appa.llm.complete", fake_complete)
    with factory() as s:
        run = runs.create(s, "scout", input={"boards": ["greenhouse:hooli"]}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.result["counts"]["manual"] == 2  # Hooli via profile, Globex via list (no model call)
    assert len(seen_prompts) == 1
    assert "Hooli" in seen_prompts[0] and "Build things" not in seen_prompts[0]  # no posting text


def test_concierge_asks_once(factory):
    with factory() as s:
        s.add_all(
            [
                db.Application(company="A", role="SWE", status="Needs Review", notes="sponsorship unclear"),
                db.Application(company="B", role="SWE", status="Apply Myself"),
                db.Application(company="C", role="SWE", status="Applied"),
            ]
        )
        s.commit()
        r1 = runs.create(s, "concierge", enqueue=False)
    done = runs.execute(factory, r1.id)
    assert done.status == "succeeded" and done.result["new_questions"] == 2
    with factory() as s:
        r2 = runs.create(s, "concierge", enqueue=False)
    assert runs.execute(factory, r2.id).result["new_questions"] == 0


def test_provider_failure_stops_run_without_marking_postings_seen(factory, scout_env, monkeypatch):
    from appa import extract

    def no_key(job, model=None):
        raise extract.ExtractionError("OPENROUTER_API_KEY is not set", fatal=True)

    monkeypatch.setattr("appa.extract.extract_with_meta", no_key)
    with factory() as s:
        run = runs.create(s, "scout", input={"boards": ["config"]}, enqueue=False)
    done = runs.execute(factory, run.id)
    assert done.status == "failed" and "OPENROUTER_API_KEY" in done.error
    with factory() as s:
        # Stopped at the first posting that needed a model: nothing was marked seen, so the
        # next run (after fixing the key) screens everything
        assert s.query(db.SeenJob).count() == 0
        assert s.query(db.Application).count() == 0
