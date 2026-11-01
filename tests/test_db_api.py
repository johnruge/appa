import pytest
from fastapi.testclient import TestClient

from appa import backfill, db
from appa.api import app as api
from appa.api import auth
from appa.config import Candidate, Config, Rules
from appa.core.models import Extraction, Job, Verdict
from appa.pipeline import Pipeline

CSV = """company,Application Status,Stage,Position,Portal,Location,Date of Application,Referal,People,todo,Notes
globex,Applied,None,swe newgrad,https://careers.globex.example/applications/dashboard,multiple,8/10/2026,yes,"A. Friend",- email recruiter,
initech ,Applied,None,swe newgrad,,multiple,9/5/2026,no,,,
Hooli,Rejected,None,"Software Engineer, Early Career (SF)",https://job-boards.greenhouse.io/hooli/jobs/1000001,"San Francisco, CA",10/8/2026,no,,,note
Pied Piper,Applied,None,"Software Engineer, Early Career",https://piedpiper.example/careers?ashby_jid=00000000-0000-4000-8000-000000000001&src=LinkedIn,SF,10/6/2026,no,,,
Vandelay,,recruiter call,swe newgrad,,ny,,,,,
Umbrella,Rejected,None,Not specified,,,,,,,
"""


@pytest.fixture()
def seeded(factory, tmp_path):
    p = tmp_path / "t.csv"
    p.write_text(CSV)
    with factory() as s:
        rep = backfill.import_csv(s, p, applied_with_email="me@university.edu")
    return rep, p


def test_backfill(factory, seeded):
    rep, path = seeded
    assert rep.imported == 6
    assert any("Vandelay has no status" in w for w in rep.warnings)
    assert any("Umbrella has no application date" in w for w in rep.warnings)
    with factory() as s:
        g = s.query(db.Application).filter_by(company="globex").one()
        assert g.referral is True and g.stage is None and str(g.date_applied) == "2026-08-10"
        assert g.applied_with_email == "me@university.edu"
        sh = s.query(db.Application).filter_by(company="Pied Piper").one()
        assert sh.job_key == "ashby:00000000-0000-4000-8000-000000000001"
        assert s.query(db.Application).filter_by(company="Vandelay").one().applied_with_email is None
        again = backfill.import_csv(s, path, applied_with_email="me@university.edu")
    assert again.imported == 0 and again.skipped_existing == 6


def test_find_match(factory, seeded):
    with factory() as s:
        # Same Greenhouse job, reached through the application-confirmation URL
        m = db.find_match(s, "x", "y", "https://job-boards.greenhouse.io/hooli/jobs/1000001/application")
        assert m.kind == "same_posting"
        # Rows with no link / job ID never count as applied: fair game to apply again
        assert db.find_match(s, "Initech, Inc.", "SWE NewGrad", None).kind == "company_tracked"
        assert (
            db.find_match(s, "globex", "swe newgrad", "https://careers.globex.example/applications/dashboard").kind
            == "company_tracked"
        )
        assert db.find_match(s, "Vandelay", "Other role", None).kind == "company_tracked"
        # A different job ID at a company with an ID'd application
        assert (
            db.find_match(
                s, "Hooli", "Software Engineer, Early Career (SF)", "https://job-boards.greenhouse.io/hooli/jobs/999"
            ).kind
            == "company_applied"
        )
        assert db.find_match(s, "Figma", "SWE", None) is None


@pytest.fixture()
def client(factory, seeded):
    from appa.api import deps

    deps.key_limiter.hits.clear()
    deps.fail_limiter.hits.clear()
    with factory() as s:
        rw = auth.create_key(s, "agent", ["applications:read", "applications:write"])
        ro = auth.create_key(s, "reader", ["applications:read"])
    return (
        TestClient(api.app, base_url="https://testserver"),
        {"Authorization": f"Bearer {rw}"},
        {"Authorization": f"Bearer {ro}"},
    )


def test_auth_required(client):
    c, _, _ = client
    assert c.get("/api/v1/health").status_code == 200
    assert c.get("/api/v1/applications").status_code == 401
    assert c.get("/api/v1/applications", headers={"Authorization": "Bearer appa_nope_nope"}).status_code == 401
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_scopes_and_revoke(client, factory):
    c, rw, ro = client
    body = {"company": "Figma", "role": "SWE New Grad"}
    assert c.post("/api/v1/applications", json=body, headers=ro).status_code == 403
    with factory() as s:
        auth.revoke(s, "agent")
    assert c.post("/api/v1/applications", json=body, headers=rw).status_code == 401


def test_create_check_update(client):
    c, rw, ro = client
    r = c.get("/api/v1/applications/check", params={"company": "Figma", "role": "SWE"}, headers=ro)
    assert r.json()["already_applied"] is False and r.json()["match"] is None

    r = c.post(
        "/api/v1/applications",
        headers=rw,
        json={
            "company": "Figma",
            "role": "Software Engineer, New Grad",
            "link": "https://job-boards.greenhouse.io/figma/jobs/555",
            "applied_with_email": "me@gmail.com",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "Applied" and created["date_applied"] and created["source"] == "api:agent"

    dup = c.post(
        "/api/v1/applications",
        headers=rw,
        json={
            "company": "figma inc",
            "role": "x",
            "link": "https://job-boards.greenhouse.io/figma/jobs/555/application",
        },
    )
    assert dup.status_code == 409 and dup.json()["application"]["id"] == created["id"]

    r = c.patch(f"/api/v1/applications/{created['id']}", headers=rw, json={"status": "Interviewing", "stage": "OA"})
    assert r.json()["status"] == "Interviewing"

    r = c.get("/api/v1/applications/check", params={"company": "Figma", "role": "Backend"}, headers=ro)
    assert r.json()["match"] == "company_applied" and r.json()["already_applied"] is False
    r = c.get(
        "/api/v1/applications/check",
        params={"company": "Figma", "link": "https://job-boards.greenhouse.io/figma/jobs/555"},
        headers=ro,
    )
    assert r.json()["already_applied"] is True and r.json()["application"]["id"] == created["id"]

    # No job ID: always recorded, even with the same company + role already on file
    for _ in range(2):
        assert (
            c.post("/api/v1/applications", headers=rw, json={"company": "initech", "role": "swe newgrad"}).status_code
            == 201
        )


@pytest.mark.parametrize(
    "body",
    [
        {"company": "", "role": "x"},
        {"company": "A", "role": "x", "status": "Hired!!"},
        {"company": "A", "role": "x", "link": "javascript:alert(1)"},
        {"company": "A", "role": "x", "applied_with_email": "nope"},
        {"company": "A", "role": "x", "is_admin": True},
        {"company": "A" * 201, "role": "x"},
    ],
)
def test_validation(client, body):
    c, rw, _ = client
    r = c.post("/api/v1/applications", headers=rw, json=body)
    assert r.status_code == 422 and "Traceback" not in r.text


def test_body_limit(client):
    c, rw, _ = client
    r = c.post("/api/v1/applications", headers=rw, content=b"x" * (api.MAX_BODY_BYTES + 1))
    assert r.status_code == 413


def test_failed_auth_rate_limited(client):
    c, _, _ = client
    codes = [c.get("/api/v1/applications", headers={"Authorization": "Bearer appa_x_y"}).status_code for _ in range(12)]
    assert codes[-1] == 429


def _ex(company: str, title: str) -> Extraction:
    return Extraction(
        company=company,
        title=title,
        location="SF",
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


@pytest.mark.parametrize(
    "company,title,link,policy,verdict",
    [
        # Initech's row has no link/job ID: fair game, no flag
        (
            "Initech",
            "swe newgrad",
            "https://jobs.lever.co/initech/11111111-1111-1111-1111-111111111111",
            "flag",
            Verdict.CANDIDATE,
        ),
        # Hooli has an application with a job ID; a different job ID is allowed by default
        (
            "Hooli",
            "Software Engineer, Early Career (NYC)",
            "https://job-boards.greenhouse.io/hooli/jobs/777",
            "allow",
            Verdict.CANDIDATE,
        ),
        (
            "Hooli",
            "Software Engineer, Early Career (NYC)",
            "https://job-boards.greenhouse.io/hooli/jobs/777",
            "flag",
            Verdict.REVIEW,
        ),
        # Same job ID as the tracked Hooli posting: the one true duplicate
        ("Hooli", "Whatever", "https://job-boards.greenhouse.io/hooli/jobs/1000001", "allow", Verdict.DUPLICATE),
    ],
)
def test_pipeline_dedupe(factory, seeded, monkeypatch, company, title, link, policy, verdict):
    cfg = Config(
        candidate=Candidate(name="t", application_email="t@x.edu"),
        rules=Rules(title_includes=["engineer", "swe"], same_company=policy),
    )
    monkeypatch.setattr("appa.extract.extract_with_meta", lambda job, _model=None: (_ex(company, title), None))
    with factory() as s:
        p = Pipeline(cfg, s)
        job = Job(company=company, title=title, url=link, ats="greenhouse", description="Build things with Python.")
        r = p.process(job)
        assert r.decision.verdict == verdict, r.decision.reasons
        assert p.seen(job.key) and p.process(job).skipped


@pytest.mark.parametrize(
    "url,key",
    [
        ("https://job-boards.greenhouse.io/hooli/jobs/1000001/application", "greenhouse:1000001"),
        ("https://www.initrode.example/company/careers?gh_jid=2000001", "greenhouse:2000001"),
        (
            "https://apply.careers.microsoft.com/candidate/login?domain=microsoft.com&pid=1111111111111111&next=%2F",
            "apply.careers.microsoft.com:1111111111111111",
        ),
        (
            "https://apply.careers.microsoft.com/careers/job/2222222222222222",
            "apply.careers.microsoft.com:2222222222222222",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Software-Engineer-I_JR-123456",
            "acme.wd1.myworkdayjobs.com:jr-123456",
        ),
        ("https://acme.wd1.myworkdayjobs.com/en-US/External/jobTasks/completed/application", None),
        ("https://www.amazon.jobs/en/jobs/10000001/software-development-engineer-2027", "amazon.jobs:10000001"),
        ("https://careers-acme.icims.com/jobs/123456/associate-software-engineer/job", "careers-acme.icims.com:123456"),
        ("https://jobs.acme.example/job/-/-/12345/98765432100", "jobs.acme.example:98765432100"),
        ("https://www.linkedin.com/jobs/view/4000000001/", "linkedin.com:4000000001"),
        ("https://www.acme.example/jobs/654321-en_US_TEAM/Associate-Software-Engineer", "acme.example:654321"),
        (
            "https://jobs.acme.example/positions/0123456789abcdef01234567/apply",
            "jobs.acme.example:0123456789abcdef01234567",
        ),
        ("https://careers.globex.example/applications/dashboard", None),
        ("https://example.com/careers/new-grad/2027", None),
        (None, None),
    ],
)
def test_job_key(url, key):
    from appa.core.jobid import job_key

    assert job_key(url) == key


def test_same_title_different_job_ids_are_different_postings(factory):
    with factory() as s:
        s.add(
            db.Application(
                company="Initrode",
                role="Software Engineer, New Grad",
                status="Applied",
                link="https://www.initrode.example/company/careers?gh_jid=2000001",
            )
        )
        s.commit()
        other = "https://www.initrode.example/company/careers?gh_jid=2000002"
        assert db.find_match(s, "Initrode", "Software Engineer, New Grad", other).kind == "company_applied"
        same = "https://job-boards.greenhouse.io/initrode/jobs/2000001"
        assert db.find_match(s, "Initrode", "Anything", same).kind == "same_posting"
        # No ID on the new link: never a duplicate
        assert db.find_match(s, "Initrode", "Software Engineer, New Grad", None).kind == "company_applied"


def test_backfill_skips_repeated_job_id(factory, tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(
        CSV.splitlines()[0] + "\n"
        "Initrode,Applied,None,SWE,https://www.initrode.example/careers?gh_jid=1,Raleigh,10/8/2026,no,,,\n"
        "Initrode,Applied,None,SWE,https://www.initrode.example/careers?gh_jid=2,San Mateo,10/8/2026,no,,,\n"
        "Initrode,Applied,None,SWE again,https://job-boards.greenhouse.io/initrode/jobs/1,Raleigh,10/9/2026,no,,,\n"
    )
    with factory() as s:
        rep = backfill.import_csv(s, p, applied_with_email=None)
    assert rep.imported == 2
    assert any("same job ID as line 2" in w for w in rep.warnings)


def test_api_conflict_only_on_job_id(client):
    c, rw, _ = client
    link = "https://careers.acme.example/careers/job/42424242"
    assert (
        c.post("/api/v1/applications", headers=rw, json={"company": "Acme", "role": "IC2", "link": link}).status_code
        == 201
    )
    r = c.post(
        "/api/v1/applications", headers=rw, json={"company": "Acme", "role": "Different title", "link": link + "?src=x"}
    )
    assert r.status_code == 409 and r.json()["match"] == "same_posting"
    # Same title, different job ID: a separate posting, accepted
    r = c.post(
        "/api/v1/applications",
        headers=rw,
        json={"company": "Acme", "role": "IC2", "link": "https://careers.acme.example/careers/job/43434343"},
    )
    assert r.status_code == 201


def test_links_without_a_job_id_pattern_are_recorded(factory, monkeypatch):
    cfg = Config(candidate=Candidate(name="t", application_email="t@x.edu"), rules=Rules(title_includes=["engineer"]))
    monkeypatch.setattr("appa.extract.extract_with_meta", lambda job, _model=None: (_ex("Acme", "Engineer"), None))
    monkeypatch.setattr("appa.extract.identify_job_id", lambda job, _model=None: (None, None))
    with factory() as s:
        p = Pipeline(cfg, s)
        p.process(
            Job(
                company="Acme",
                title="Engineer",
                url="https://acme.example/careers/new-grad/2027",
                description="Build things.",
            )
        )
        p.process(
            Job(
                company="Hooli",
                title="Engineer",
                url="https://job-boards.greenhouse.io/hooli/jobs/555",
                ats="greenhouse",
                description="Build things.",
            )
        )
        rows = s.scalars(db.select(db.Observation)).all()
        assert [(o.kind, o.site, o.subject) for o in rows] == [
            ("id_not_in_link", "acme.example", "/careers/new-grad/{n}")
        ]
        assert rows[0].detail["id_source"] == "none"
