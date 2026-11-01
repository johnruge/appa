"""Job IDs for any site: link patterns, page structured data, verified AI reads, IDs you or
your agent provide, and IDs in your own notes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from appa import backfill, db, extract
from appa.core.jobid import clean_id, ids_in_text, resolve_key, verified
from appa.core.models import Job
from appa.sources.page_ids import find_in_page


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("R-12345", "r-12345"),
        ("JR-2026-0042", "jr-2026-0042"),
        ("1234567", "1234567"),
        ("engineer", None),
        ("2027", None),
        ("", None),
        ("<script>", None),
    ],
)
def test_clean_id(raw, ok):
    assert clean_id(raw) == ok


def test_resolve_key_scopes():
    assert resolve_key("https://job-boards.greenhouse.io/hooli/jobs/123", "999") == "greenhouse:123"  # link wins
    assert resolve_key("https://careers.acme.example/jobs/software-engineer", "R-77") == "careers.acme.example:r-77"
    assert resolve_key(None, "10567489", "amazon (audible)") == "co:amazon:10567489"
    assert resolve_key("https://careers.acme.example/jobs/software-engineer") is None


def test_verified_needs_the_exact_id_on_the_page():
    page = "Software Engineer · Requisition R-12345 · Chicago"
    assert verified("R-12345", "https://x.example/job", page)
    assert not verified("R-1234", "https://x.example/job", page)  # partial match isn't enough
    assert not verified("R-99999", "https://x.example/job", page)  # invented


PAGE_LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"SWE","identifier":{"@type":"PropertyValue","name":"Acme","value":"REQ-4410"}}
</script></head><body>x</body></html>"""


def test_page_structured_data():
    assert find_in_page("https://careers.acme.example/x", PAGE_LD) == ("req-4410", "page:json-ld")
    canon = '<link rel="canonical" href="https://www.amazon.jobs/en/jobs/10583340/sde">'
    assert find_in_page("https://amazon.example/share", canon) == ("10583340", "page:canonical")
    attr = '<div data-job-id="88123">apply</div>'
    assert find_in_page("https://x.example/j", attr) == ("88123", "page:attribute")
    assert find_in_page("https://x.example/j", "<p>no id here</p>") == (None, None)


def test_ai_id_is_discarded_unless_it_is_on_the_page(monkeypatch):
    from appa import llm

    job = Job(
        company="Acme",
        title="SWE",
        url="https://careers.acme.example/jobs/swe",
        description="Software engineer. Job number: 55-AB-901. Apply now.",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    def fake(answer):
        return lambda *a, **k: llm.Completion(model="t", parsed=extract._FoundId(job_id=answer, where="text"))

    monkeypatch.setattr("appa.llm.complete", fake("55-AB-901"))
    assert extract.identify_job_id(job)[0] == "55-ab-901"
    monkeypatch.setattr("appa.llm.complete", fake("55-AB-902"))  # hallucinated
    assert extract.identify_job_id(job)[0] is None
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr("appa.extract.load_env", lambda: None, raising=False)
    import appa.config

    monkeypatch.setattr(appa.config, "load_env", lambda *a, **k: None)
    assert extract.identify_job_id(job) == (None, None)  # no key: skipped quietly


def test_ids_in_your_notes():
    assert ids_in_text("amazon (10567489)") == ["10567489"]
    assert ids_in_text("Job ID 14930. New College Grad role") == ["14930"]
    assert ids_in_text("Requisition 2026-127949; full-time") == ["2026-127949"]
    assert ids_in_text("Requisition JR-259943. User supplied confirmation") == ["jr-259943"]
    assert ids_in_text("graduating 2027, 3 years") == []


def test_notes_ids_never_reuse_another_rows_id(factory):
    with factory() as s:
        s.add_all(
            [
                db.Application(
                    company="GlobalFoundries",
                    role="NCG",
                    status="Applied",
                    link="https://careers.gf.com/careers/job/563980770505837",
                ),
                db.Application(
                    company="GlobalFoundries",
                    role="Workday one",
                    status="Applied",
                    link="https://globalfoundries.wd1.myworkdayjobs.com/en-US/External/jobTasks/completed/application",
                    notes="separate from the October 9 entry (posting 563980770505837)",
                ),
                db.Application(company="amazon (10567489)", role="swe newgrad", status="Applied"),
            ]
        )
        s.commit()
        rows = {r[1]: r[3] for r in backfill.ids_from_notes(s, apply=True)}
        assert rows["GlobalFoundries"].startswith("skipped") and rows["amazon (10567489)"] == "set"
        # Later, the same Amazon job arrives through amazon.jobs: still recognized
        m = db.find_match(s, "Amazon", "SDE", "https://www.amazon.jobs/en/jobs/10567489/sde")
        assert m and m.kind == "same_posting"


def test_agents_can_record_an_application_with_its_job_id(factory):
    from appa.api import app as api
    from appa.api import auth, deps

    deps.key_limiter.hits.clear()
    with factory() as s:
        key = auth.create_key(s, "my-agent", ["applications:read", "applications:write"])
    c = TestClient(api.app, base_url="https://testserver")
    h = {"Authorization": f"Bearer {key}"}
    body = {"company": "Acme", "role": "SWE", "link": "https://careers.acme.example/apply/thanks", "job_id": "REQ-4410"}
    r = c.post("/api/v1/applications", headers=h, json=body)
    assert r.status_code == 201 and r.json()["job_key"] == "careers.acme.example:req-4410"
    assert r.json()["job_id_source"] == "provided"
    assert c.post("/api/v1/applications", headers=h, json={**body, "role": "Different title"}).status_code == 409
    chk = c.get("/api/v1/applications/check", headers=h, params={"company": "Acme", "job_id": "REQ-4410"}).json()
    assert chk["already_applied"] is True
    assert c.post("/api/v1/applications", headers=h, json={**body, "job_id": "<script>"}).status_code == 422
