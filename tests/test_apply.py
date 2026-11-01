"""Applying: field matching, planning, and the full approve-before-submit flow through the
API with a simulated runner. Postgres-backed; no browser, no network, no model calls."""

from __future__ import annotations

import io
import secrets

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from appa import db
from appa.api import app as api
from appa.api import auth, deps, users
from appa.api.routes import auth as auth_routes
from appa.apply import service
from appa.apply.planner import build_plan
from appa.config import Candidate, Config, Rules
from appa.core.apply_fields import FormQuestion, match_option, must_ask, saved_key, standard_key

Q = lambda **kw: FormQuestion(**{"ref": "id:x", "label": "", "kind": "text", **kw})  # noqa: E731


# ---- matching labels seen on real Greenhouse / Lever / Ashby forms ---------------------


@pytest.mark.parametrize(
    "label,kind,key",
    [
        ("First Name*", "text", "first_name"),
        ("Legal Name", "text", "full_name"),
        ("Full name✱", "text", "full_name"),
        ("Preferred Name | What would you like us to call you?", "text", "preferred_name"),
        ("Name Pronunciation | How do you pronounce your name?", "text", None),
        ("Pronouns", "combobox", "pronouns"),
        ("LinkedIn Profile", "text", "linkedin_url"),
        ("Are you authorized to work in the country for which you applied?", "combobox", "work_authorized"),
        (
            "Will you now or in the future require sponsorship for employment visa status?",
            "radio",
            "requires_sponsorship",
        ),
        (
            "Have you ever worked for Figma before, as an employee or a contractor/consultant?",
            "combobox",
            "previously_employed",
        ),
        ("Please tell us how you heard about this opportunity.", "select", "how_heard"),
        ("Are you Hispanic/Latino?", "combobox", "hispanic_latino"),
        ("From where do you intend to work?", "text", "work_location"),
        ("Location (City)", "autocomplete", "location"),
    ],
)
def test_standard_keys(label, kind, key):
    assert standard_key(Q(label=label, kind=kind)) == key


@pytest.mark.parametrize(
    "label",
    [
        "Are you legally authorized to work in the location you are applying without future sponsorship requirements?",
        "Are you authorized to work in the US without requiring visa sponsorship now or in the future?",
    ],
)
def test_compound_authorization_questions_are_never_mapped(label):
    # "Yes" here means "I do NOT need sponsorship": mapping it to requires_sponsorship would invert it.
    q = Q(label=label, kind="combobox", required=True, options=["Yes", "No"])
    assert standard_key(q) is None
    plan = build_plan([q], {"requires_sponsorship": "Yes", "work_authorized": "Yes"})
    assert not plan.ready and plan.missing[0].question.label == label


@pytest.mark.parametrize(
    "label,key",
    [
        ("Will you now or in the future require sponsorship for employment visa status?", "requires_sponsorship"),
        ("Will you require visa sponsorship in the future?", "requires_sponsorship"),
        ("Do you currently require visa sponsorship?", "sponsorship_now"),
        ("Will you require sponsorship to begin employment?", "sponsorship_now"),
        ("Do you require sponsorship?", None),  # doesn't say now or future: asks you
        ("Are you legally authorized to work in the US?", "work_authorized"),
    ],
)
def test_sponsorship_now_vs_future(label, key):
    assert standard_key(Q(label=label, kind="radio", options=["Yes", "No"])) == key


def test_authorized_now_but_future_sponsorship():
    bank = {
        "work_authorized": "Yes — authorized under applicable F-1 employment authorization",
        "requires_sponsorship": "Yes",
        "sponsorship_now": "No",
    }
    qs = [
        Q(
            ref="a",
            label="Are you legally authorized to work in the United States?",
            kind="radio",
            options=["Yes", "No"],
            required=True,
        ),
        Q(
            ref="b",
            label="Will you now or in the future require sponsorship?",
            kind="radio",
            options=["Yes", "No"],
            required=True,
        ),
        Q(ref="c", label="Do you currently require sponsorship?", kind="radio", options=["Yes", "No"], required=True),
    ]
    assert {i.ref: i.value for i in build_plan(qs, bank).items} == {"a": "Yes", "b": "Yes", "c": "No"}


def test_files_and_consent_radios():
    assert standard_key(Q(label="", kind="file")) is None  # unidentified: nothing gets attached
    assert standard_key(Q(ref="id:upload_3", label="Attach", kind="file")) is None
    assert standard_key(Q(ref="id:x", label="Resume/CV", kind="file")) == "resume"
    # Greenhouse: both file inputs have blank labels; the ids tell them apart
    assert standard_key(Q(ref="id:resume", label="", kind="file")) == "resume"
    assert standard_key(Q(ref="id:cover_letter", label="", kind="file")) == "cover_letter"
    assert (
        build_plan([Q(ref="id:cover_letter", label="", kind="file")], {}).items[0].value is None
    )  # optional: left empty
    required_cl = build_plan(
        [Q(ref="id:cover_letter", label="Cover Letter", kind="file", required=True)], {"resume": "x"}
    )
    assert not required_cl.ready  # required cover letter: asks you, never the résumé
    assert standard_key(Q(label="Cover Letter", kind="file")) == "cover_letter"
    sms = Q(
        label="Phone", kind="radio", options=["Yes - I consent to receiving SMS text messages", "No - I do not consent"]
    )
    assert standard_key(sms) == "sms_consent"


@pytest.mark.parametrize(
    "label,kind,asks",
    [
        ("Why do you want to join Figma?", "textarea", True),
        ("Here's some text encoded in a common format. Figure out the correct secret", "textarea", True),
        ("Are you a human?", "radio", True),
        ("As part of our interview process, we may use AI notetakers", "radio", True),
        ("LinkedIn Profile", "text", False),
    ],
)
def test_must_ask(label, kind, asks):
    assert bool(must_ask(Q(label=label, kind=kind))) is asks


@pytest.mark.parametrize(
    "value,options,expected",
    [
        ("Yes", ["Yes", "No"], "Yes"),
        ("no", ["Yes, I consent", "No, I do not consent"], "No, I do not consent"),
        ("Decline", ["Male", "Female", "Decline To Self Identify"], "Decline To Self Identify"),
        (
            "Decline",
            ["I am not a protected veteran", "I identify as a protected veteran", "I don't wish to answer"],
            "I don't wish to answer",
        ),
        ("United States", ["United States +1", "Afghanistan +93"], "United States +1"),
        ("Maybe", ["Yes", "No"], None),  # never guesses
        ("", ["Yes", "No"], None),
    ],
)
def test_match_option(value, options, expected):
    assert match_option(value, options) == expected


GREENHOUSE_FORM = [
    Q(ref="id:first_name", label="First Name", required=True),
    Q(ref="id:last_name", label="Last Name", required=True),
    Q(ref="id:email", label="Email", kind="email", required=True),
    Q(ref="id:country", label="Country", kind="combobox", required=True, options=["United States +1", "Canada +1"]),
    Q(ref="id:resume", label="", kind="file"),
    Q(ref="id:q1", label="Why do you want to join Hooli?", kind="textarea", required=True),
    Q(ref="id:q2", label="Additional Information", kind="textarea"),
    Q(
        ref="id:q3",
        label="Are you authorized to work in the country for which you applied?",
        kind="combobox",
        required=True,
        options=["Yes", "No"],
    ),
    Q(ref="id:gender", label="Gender", kind="combobox", options=["Male", "Female", "Decline To Self Identify"]),
    Q(
        ref="id:school",
        label="Which university did you attend?",
        kind="select",
        required=True,
        options=["Aalborg University"],
        options_complete=False,
    ),
]
ANSWERS = {
    "first_name": "Pat",
    "last_name": "Doe",
    "email": "pat@example.com",
    "country": "United States",
    "work_authorized": "Yes",
    "gender": "Decline",
    "school": "University of Chicago",
}


def test_plan_fills_from_bank_and_asks_for_essays():
    plan = build_plan(GREENHOUSE_FORM, ANSWERS)
    values = {i.ref: i.value for i in plan.items}
    assert values["id:first_name"] == "Pat" and values["id:country"] == "United States +1"
    assert values["id:q3"] == "Yes" and values["id:gender"] == "Decline To Self Identify"
    assert values["id:resume"] == "@resume"
    assert values["id:school"] == "University of Chicago"  # long list: the runner verifies it exists
    assert values["id:q2"] is None  # optional essay: left empty, not invented
    assert [m.question.ref for m in plan.missing] == ["id:q1"]
    # Once you answer it, the same question on any form is filled from your saved answer
    plan2 = build_plan(GREENHOUSE_FORM, {**ANSWERS, saved_key("Why do you want to join Hooli?"): "Because…"})
    assert plan2.ready


def test_plan_model_fallback_can_only_pick_existing_answers():
    q = Q(ref="id:q9", label="Do you need a visa to work here?", kind="radio", required=True, options=["Yes", "No"])
    assert (
        build_plan([q], {"requires_sponsorship": "Yes"}, choose=lambda q, a: "requires_sponsorship").items[0].value
        == "Yes"
    )
    assert not build_plan([q], {"requires_sponsorship": "Yes"}, choose=lambda q, a: "made_up_key").ready
    assert not build_plan([q], {}, choose=None).ready


def test_apply_url():
    assert service.apply_url("https://job-boards.greenhouse.io/hooli/jobs/123") == (
        "greenhouse",
        "https://job-boards.greenhouse.io/hooli/jobs/123",
    )
    assert service.apply_url("https://jobs.lever.co/hooli/00000000-0000-4000-8000-000000000001")[1].endswith("/apply")
    assert service.apply_url("https://jobs.ashbyhq.com/hooli/00000000-0000-4000-8000-000000000001/application")[
        1
    ].endswith("/application")
    # Any other public job page: opened as-is, the runner finds the form
    assert service.apply_url("https://hooli.example/careers?gh_jid=123") == (
        "generic",
        "https://hooli.example/careers?gh_jid=123",
    )
    assert service.apply_url("https://apply.workable.com/acme/j/ABC123/")[0] == "generic"
    # Sites that need an account, or forbid automation: yours
    for link, why in [
        ("https://acme.wd1.myworkdayjobs.com/x/job/SWE_JR-1", "Workday"),
        ("https://careers-acme.icims.com/jobs/1/job", "iCIMS"),
        ("https://www.linkedin.com/jobs/view/1", "LinkedIn"),
    ]:
        with pytest.raises(service.ApplyError, match=why):
            service.apply_url(link)
    with pytest.raises(service.ApplyError, match="confirmation or dashboard"):
        service.apply_url("https://careers.acme.example/applications/dashboard")


# ---- the full flow through the API ----------------------------------------------------

PASSWORD = secrets.token_urlsafe(18)
FORM = [
    {"ref": "id:first_name", "label": "First Name", "kind": "text", "required": True},
    {"ref": "id:email", "label": "Email", "kind": "email", "required": True},
    {
        "ref": "id:q1",
        "label": "Are you authorized to work in the US?",
        "kind": "radio",
        "required": True,
        "options": ["Yes", "No"],
    },
    {"ref": "id:q2", "label": "Why Hooli?", "kind": "textarea", "required": True},
]


def png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (60, 40), (1, 2, 3)).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture()
def env(factory, tmp_path, monkeypatch):
    for lim in (deps.key_limiter, deps.user_limiter, deps.fail_limiter, auth_routes.login_limiter):
        lim.hits.clear()
    monkeypatch.setenv("APPA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        "appa.config.load",
        lambda path=None: Config(candidate=Candidate(name="t", application_email="t@x.edu"), rules=Rules()),
    )
    with factory() as s:
        users.create_user(s, "me", PASSWORD)
        runner_key = auth.create_key(s, "my-mac", ["runner"])
        other_key = auth.create_key(s, "bot", ["runs:read", "runs:write", "applications:write"])
        app = db.Application(
            company="Hooli", role="SWE", status="To Apply", link="https://job-boards.greenhouse.io/hooli/jobs/4242"
        )
        s.add(app)
        s.commit()
        for k, v in {"first_name": "Pat", "email": "pat@example.com", "work_authorized": "Yes"}.items():
            service.set_answer(s, k, v)
        app_id = app.id
    c = TestClient(api.app, base_url="https://testserver")
    r = c.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD})
    user = {"X-CSRF-Token": r.json()["csrf_token"]}
    return c, user, {"Authorization": f"Bearer {runner_key}"}, {"Authorization": f"Bearer {other_key}"}, app_id


def test_full_flow_prefill_then_you_submit(env, factory):
    c, user, runner, _, app_id = env
    r = c.post(f"/api/v1/applications/{app_id}/apply", headers=user)
    assert r.status_code == 201 and r.json()["apply_url"] == "https://job-boards.greenhouse.io/hooli/jobs/4242"
    aid = r.json()["id"]
    assert c.post(f"/api/v1/applications/{app_id}/apply", headers=user).status_code == 422  # one at a time

    # 1. Runner opens the form; the essay isn't in the bank, so it stops and asks you
    job = c.get("/api/v1/runner/next", headers=runner).json()
    assert job["phase"] == "prepare" and "plan" not in job
    reply = c.post(
        f"/api/v1/runner/attempts/{aid}/form", headers=runner, json={"questions": FORM, "page_url": "x"}
    ).json()
    assert reply["action"] == "stop"
    c.post(f"/api/v1/runner/jobs/{job['job_id']}/done", headers=runner)
    essay = next(q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_field")
    assert essay["context"]["label"] == "Why Hooli?"

    # 2. You answer; it's saved and the attempt goes back to the runner
    r = c.post(
        f"/api/v1/questions/{essay['id']}/answer", headers=user, json={"action": "answer", "choice": "I like search."}
    )
    assert r.json()["resumed_attempts"] == [aid]
    job = c.get("/api/v1/runner/next", headers=runner).json()
    reply = c.post(
        f"/api/v1/runner/attempts/{aid}/form", headers=runner, json={"questions": FORM, "page_url": "x"}
    ).json()
    assert reply["action"] == "fill"
    values = {i["ref"]: i["value"] for i in reply["plan"]}
    assert values == {"id:first_name": "Pat", "id:email": "pat@example.com", "id:q1": "Yes", "id:q2": "I like search."}

    # 3. Filled + screenshot -> ready: the tab is yours. appa never submits.
    up = c.post(
        f"/api/v1/runner/attempts/{aid}/screenshot", headers=runner, files={"file": ("p.png", png(), "image/png")}
    ).json()
    report = [{"ref": k, "label": k, "ok": True} for k in values]
    c.post(
        f"/api/v1/runner/attempts/{aid}/preview",
        headers=runner,
        json={"report": report, "screenshot_upload_id": up["upload_id"]},
    )
    c.post(f"/api/v1/runner/jobs/{job['job_id']}/done", headers=runner)
    assert c.get("/api/v1/runner/next", headers=runner).status_code == 204  # nothing more for the runner, ever
    assert c.post(f"/api/v1/runner/attempts/{aid}/result", headers=runner, json={}).status_code in (404, 405)
    card = next(q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_ready")
    assert card["context"]["preview_upload_id"] == up["upload_id"] and not card["context"]["required_unfilled"]
    assert "submit it there yourself" in card["prompt"]
    assert c.get(f"/api/v1/uploads/{up['upload_id']}").headers["content-type"] == "image/png"
    with factory() as s:
        assert s.get(db.Application, app_id).status == "To Apply"  # not Applied until you say so

    # 4. You submitted it in the browser and say so -> Applied
    r = c.post(f"/api/v1/questions/{card['id']}/answer", headers=user, json={"action": "applied"})
    assert r.status_code == 200
    with factory() as s:
        app = s.get(db.Application, app_id)
        assert app.status == "Applied" and app.applied_with_email == "pat@example.com" and app.date_applied
        attempt = s.get(db.ApplyAttempt, aid)
        assert attempt.status == "applied" and s.get(db.Run, attempt.run_id).status == "succeeded"


def _to_ready(c, user, runner, app_id, report_ok=True):
    aid = c.post(f"/api/v1/applications/{app_id}/apply", headers=user).json()["id"]
    job = c.get("/api/v1/runner/next", headers=runner).json()
    form = FORM[:3]
    c.post(f"/api/v1/runner/attempts/{aid}/form", headers=runner, json={"questions": form, "page_url": "x"})
    report = [
        {
            "ref": q["ref"],
            "label": q["label"],
            "ok": report_ok or q["ref"] != "id:email",
            "error": None if report_ok else "boom",
        }
        for q in form
    ]
    c.post(f"/api/v1/runner/attempts/{aid}/preview", headers=runner, json={"report": report})
    c.post(f"/api/v1/runner/jobs/{job['job_id']}/done", headers=runner)
    return aid


def test_required_fields_that_didnt_fill_are_flagged_on_the_card(env):
    c, user, runner, _, app_id = env
    _to_ready(c, user, runner, app_id, report_ok=False)
    card = next(q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_ready")
    assert card["context"]["required_unfilled"] and "including required ones" in card["prompt"]


def test_mark_applied_works_without_a_prefill_and_is_humans_only(env, factory):
    c, user, runner, bot, app_id = env
    assert c.post(f"/api/v1/applications/{app_id}/applied", headers=bot).status_code == 403
    assert c.post(f"/api/v1/applications/{app_id}/applied", headers=runner).status_code == 403
    r = c.post(f"/api/v1/applications/{app_id}/applied", headers=user)
    assert r.status_code == 200 and r.json()["status"] == "Applied"


def test_skipping_a_ready_card_cancels_the_prefill(env, factory):
    c, user, runner, _, app_id = env
    aid = _to_ready(c, user, runner, app_id)
    card = next(q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_ready")
    c.post(f"/api/v1/questions/{card['id']}/answer", headers=user, json={"action": "skip"})
    with factory() as s:
        assert s.get(db.ApplyAttempt, aid).status == "cancelled"
        assert s.get(db.Application, app_id).status == "To Apply"


def test_runner_endpoints_are_runner_only(env):
    c, user, runner, other, app_id = env
    assert c.get("/api/v1/runner/next", headers=other).status_code == 403  # other keys
    assert c.get("/api/v1/runner/next").status_code == 403  # your browser session
    assert c.get("/api/v1/answers", headers=runner).status_code == 403  # runner can't read the bank directly
    assert c.post(f"/api/v1/applications/{app_id}/apply", headers=runner).status_code == 403
    assert (
        c.post("/api/v1/runs", headers=user, json={"role": "applier", "input": {"application_id": app_id}}).status_code
        == 422
    )


def test_cancel_and_answers_bank(env, factory):
    c, user, runner, _, app_id = env
    aid = c.post(f"/api/v1/applications/{app_id}/apply", headers=user).json()["id"]
    assert c.post(f"/api/v1/apply/attempts/{aid}/cancel", headers=user).json()["status"] == "cancelled"
    assert c.get("/api/v1/runner/next", headers=runner).status_code == 204  # its queued job was retired
    assert c.put("/api/v1/answers", headers=user, json={"key": "last_name", "value": "Doe"}).status_code == 200
    assert c.put("/api/v1/answers", headers=user, json={"key": "Bad Key!", "value": "x"}).status_code == 422
    assert c.get("/api/v1/answers").json()["answers"]["last_name"] == "Doe"


def test_start_refuses_unsupported_or_applied(env, factory):
    c, user, _, _, app_id = env
    with factory() as s:
        wd = db.Application(
            company="Acme", role="SWE", status="To Apply", link="https://acme.wd1.myworkdayjobs.com/x/job/SWE_JR-1"
        )
        done = db.Application(
            company="Initech",
            role="SWE",
            status="Applied",
            link="https://jobs.lever.co/initech/00000000-0000-4000-8000-000000000009",
        )
        s.add_all([wd, done])
        s.commit()
        wd_id, done_id = wd.id, done.id
    assert "apply yourself" in c.post(f"/api/v1/applications/{wd_id}/apply", headers=user).json()["error"]
    assert "already Applied" in c.post(f"/api/v1/applications/{done_id}/apply", headers=user).json()["error"]


def test_answering_a_standard_question_fills_the_standard_key(env, factory):
    c, user, runner, _, app_id = env
    with factory() as s:
        s.delete(s.get(db.Answer, "work_authorized"))
        s.commit()
    aid = c.post(f"/api/v1/applications/{app_id}/apply", headers=user).json()["id"]
    c.get("/api/v1/runner/next", headers=runner)
    c.post(f"/api/v1/runner/attempts/{aid}/form", headers=runner, json={"questions": FORM[:3], "page_url": "x"})
    q = next(q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_field")
    assert q["context"]["standard_key"] == "work_authorized"
    c.post(f"/api/v1/questions/{q['id']}/answer", headers=user, json={"action": "answer", "choice": "Yes"})
    answers = c.get("/api/v1/answers").json()["answers"]
    assert answers["work_authorized"] == "Yes"  # so "Are you legally authorized…" elsewhere won't ask again


def test_redo_refills_until_applied(env, factory):
    c, user, runner, _, app_id = env
    aid = _to_ready(c, user, runner, app_id)
    assert c.post(f"/api/v1/apply/attempts/{aid}/redo", headers=user).json()["status"] == "preparing"
    assert not [q for q in c.get("/api/v1/questions").json()["questions"] if q["kind"] == "apply_ready"]
    assert c.get("/api/v1/runner/next", headers=runner).json()["phase"] == "prepare"
    with factory() as s:
        a = s.get(db.ApplyAttempt, aid)
        a.status = "applied"
        s.commit()
    assert c.post(f"/api/v1/apply/attempts/{aid}/redo", headers=user).status_code == 422


def test_appa_never_submits(env, factory):
    """A perfectly filled form is handed to you, and nothing anywhere can submit it: no submit phase, no
    submit endpoint, no code that clicks a submit button."""
    import inspect

    from appa.api.routes import runner as runner_routes
    from appa.apply import service as svc
    from appa.runner import client, dom
    from appa.runner import main as runner_main

    c, user, runner, _, app_id = env
    aid = _to_ready(c, user, runner, app_id)  # every field filled ok
    with factory() as s:
        assert s.get(db.ApplyAttempt, aid).status == "ready"
        assert {j.payload.get("phase") for j in s.query(db.QueueJob) if j.kind == "apply"} == {"prepare"}
    assert c.get("/api/v1/runner/next", headers=runner).status_code == 204
    assert not hasattr(svc, "approve") and not hasattr(svc, "receive_result")
    assert not hasattr(dom, "submit") and not hasattr(dom, "confirmation")
    assert not hasattr(client.RunnerClient, "result")
    assert "result" not in {r.name for r in runner_routes.router.routes}
    source = inspect.getsource(runner_main) + inspect.getsource(dom)
    assert ".submit(" not in source and "dom.submit" not in source


def test_cover_letters_only_when_required():
    optional = Q(ref="id:cover_letter", label="Cover Letter", kind="file")
    required = Q(ref="id:cover_letter", label="Cover Letter", kind="file", required=True)
    bank = {"first_name": "Pat"}
    assert build_plan([optional], bank).items[0].value is None  # ignored
    assert not build_plan([required], bank).ready  # comes to you
    essay_cl = Q(ref="id:q", label="Cover letter (optional)", kind="textarea")
    assert build_plan([essay_cl], bank).items[0].value is None  # optional written one: ignored too


# ---- misses and failures are recorded for `appa insights` --------------------------------


def test_failures_are_recorded_without_your_answers(env, factory):
    from appa import observe

    c, user, runner, _, app_id = env
    aid = _to_ready(c, user, runner, app_id, report_ok=False)  # email didn't fill
    c.post(f"/api/v1/apply/attempts/{aid}/cancel", headers=user)
    aid2 = c.post(f"/api/v1/applications/{app_id}/apply", headers=user).json()["id"]
    c.get("/api/v1/runner/next", headers=runner)
    c.post(f"/api/v1/runner/attempts/{aid2}/blocked", headers=runner, json={"reason": "this application needs a login"})
    with factory() as s:
        rows = s.scalars(db.select(db.Observation).order_by(db.Observation.id)).all()
        assert {(o.kind, o.subject) for o in rows} >= {
            ("fill_failed", "Email"),
            ("blocked", "this application needs a login"),
        }
        assert all(o.site == "job-boards.greenhouse.io" for o in rows)
        stored = str([(o.subject, o.detail) for o in rows])
        assert "pat@example.com" not in stored and "Pat" not in stored  # answers never land here
        top = observe.summary(s)
        assert top and top[0]["n"] >= 1 and observe.counts(s)["blocked"] == 1


def test_unmapped_labels_are_recorded(env, factory):
    c, user, runner, _, app_id = env
    aid = c.post(f"/api/v1/applications/{app_id}/apply", headers=user).json()["id"]
    c.get("/api/v1/runner/next", headers=runner)
    form = [
        {
            "ref": "id:q9",
            "label": "Which team interests you most?",
            "kind": "select",
            "required": True,
            "options": ["Infra", "Data"],
        }
    ]
    c.post(f"/api/v1/runner/attempts/{aid}/form", headers=runner, json={"questions": form, "page_url": "x"})
    with factory() as s:
        o = s.scalars(db.select(db.Observation).where(db.Observation.kind == "label_unmapped")).one()
        assert o.subject == "Which team interests you most?" and o.detail["options"] == "Infra; Data"


def test_path_shape_groups_similar_links():
    from appa.observe import path_shape

    assert path_shape("https://x.com/careers/job/12345-swe") == "/careers/job/{n}-swe"
    assert path_shape("https://x.com/j/0f8fad5b-d9cb-469f-a165-70867728950e") == "/j/{uuid}"


# ---- you can take any application over ------------------------------------------------------


def test_takeover_stops_the_attempt_and_hands_it_to_you(env, factory):
    c, user, runner, bot, app_id = env
    aid = _to_ready(c, user, runner, app_id)
    assert c.post(f"/api/v1/applications/{app_id}/takeover", headers=bot).status_code == 403  # humans only
    r = c.post(f"/api/v1/applications/{app_id}/takeover", headers=user)
    assert r.status_code == 200 and r.json() == {
        "id": app_id,
        "status": "Apply Myself",
        "link": "https://job-boards.greenhouse.io/hooli/jobs/4242",
    }
    with factory() as s:
        assert s.get(db.ApplyAttempt, aid).status == "cancelled"
        assert "took over" in s.get(db.Application, app_id).notes
    assert not [q for q in c.get("/api/v1/questions").json()["questions"] if q["context"].get("attempt_id") == aid]


def test_no_takeover_once_applied(env):
    c, user, _, _, app_id = env
    c.post(f"/api/v1/applications/{app_id}/applied", headers=user)
    r = c.post(f"/api/v1/applications/{app_id}/takeover", headers=user)
    assert r.status_code == 422 and "already Applied" in r.json()["error"]


def test_listings_inbox_track_skip_restore(env, factory):
    from appa.feeds import store
    from tests.test_feeds_core import TODAY, L, cfg

    c, user, _, bot, _ = env
    assert c.get("/api/v1/listings", headers=bot).status_code == 403  # needs applications:read
    with factory() as s:
        store.ingest(
            s,
            [
                L(company="Acme", url="https://job-boards.greenhouse.io/acme/jobs/5"),
                L(company="Acme", title="Senior Engineer", url="https://job-boards.greenhouse.io/acme/jobs/6"),
            ],
            "simplify_newgrad",
            cfg(),
            today=TODAY,
        )
    rows = c.get("/api/v1/listings").json()["listings"]
    by_status = {r["status"]: r for r in rows}
    assert set(by_status) == {"new", "filtered"} and "description" not in by_status["new"]
    assert (
        c.get("/api/v1/listings?source=simplify_newgrad&status=filtered").json()["listings"][0]["title"]
        == "Senior Engineer"
    )
    assert c.get("/api/v1/listings?source=bad source").status_code == 422
    new, filtered = by_status["new"]["id"], by_status["filtered"]["id"]
    r = c.post(f"/api/v1/listings/{new}/track", headers=user)
    assert r.status_code == 201 and r.json()["created"]
    assert c.post(f"/api/v1/listings/{new}/track", headers=user).json()["created"] is False  # once only
    assert c.post(f"/api/v1/listings/{filtered}/restore", headers=user).json()["status"] == "new"
    assert c.post(f"/api/v1/listings/{filtered}/skip", headers=user).json()["status"] == "skipped"
    with factory() as s:
        app = s.get(db.Application, r.json()["application_id"])
        assert app.status == "To Apply" and app.job_key == "greenhouse:5"
    feeds_info = c.get("/api/v1/feeds").json()
    assert "simplify_newgrad" in {f["name"] for f in feeds_info["lists"]} and feeds_info["inbox"]["screened"] == 1
