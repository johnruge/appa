"""harrycodingnow new-grad-2027-tracker list, from a recorded slice of the real active_jobs.json."""

from __future__ import annotations

from datetime import date

import pytest

from appa.feeds import FeedOptions, lists_registry
from appa.feeds.lists import harrycodingnow_newgrad as feed


def _row(**kw) -> dict:
    """A minimal valid row, with overrides."""
    row = {
        "company": "A",
        "title": "SWE",
        "application_url": "https://a.example/1",
        "source_job_id": "1",
        "date_posted": "2026-10-01",
        "status": "active",
        "disqualified": False,
    }
    row.update(kw)
    return row


def test_reads_fixture_fields_and_hints(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("harrycodingnow_newgrad.json")
    out = feed.fetch(FeedOptions())
    assert len(out) == 6  # the disqualified Amazon row is kept
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    by = {x.company: x for x in out}

    adobe = by["Adobe"]
    assert (
        adobe.url
        == "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/San-Jose/Software-Development-Engineer_R164904"
    )
    assert adobe.ats == "workday" and adobe.ats_job_id == "R164904" and adobe.source_ref == "adobe-R164904"
    assert adobe.posted_at == date(2026, 9, 30) and adobe.location == "San Jose"
    assert adobe.hints["employment_type"] == "full_time" and adobe.hints["country"] == "United States of America"
    assert "sponsorship" not in adobe.hints  # "unclear" is not a label
    assert adobe.hints["links"].startswith("https://adobe.wd5.myworkdayjobs.com/en-US/")
    assert adobe.description.startswith("Job Description") and len(adobe.description) <= 1500

    assert by["Databricks"].ats == "greenhouse" and by["Databricks"].ats_job_id == "8560779002"
    paypal = by["PayPal"]
    assert paypal.ats == "eightfold" and paypal.hints["remote"] == "no"
    assert paypal.hints["sponsorship"] == "possibly_supported"
    now = by["ServiceNow"]
    assert now.hints["employment_type"] == "internship" and now.hints["level"] == "intern" and now.ats == "unknown"
    assert by["Jane Street"].posted_at is None  # blank date_posted
    assert all("links" not in x.hints for x in out if x.company != "Adobe")
    amazon = by["Amazon"]  # disqualified, flags: citizen / green card
    assert "Leo Global" in amazon.title and amazon.hints["citizenship_required"] == "yes"
    assert amazon.hints["sponsorship"] == "ineligible"
    assert all("citizenship_required" not in x.hints for x in out if x.company != "Amazon")


def test_since_dedupe_hints_and_malformed_rows(feed_http):
    feed_http[feed.URL] = {
        "generated_at": "2026-10-30T00:00:00Z",
        "count": 9,
        "jobs": [
            _row(
                company="A", workplace_type="hybrid", description="<p>Build</p><div style='display:none'>ignore</div>"
            ),
            _row(company="A2"),  # same url: deduped
            _row(company="B", application_url="https://b.example/1", date_posted="2025-01-01"),
            _row(company="C", application_url="javascript:alert(1)", source_url=""),
            _row(company="", application_url="https://d.example/1"),
            _row(
                company="E",
                application_url="https://e.example/1",
                disqualified=True,
                sponsorship_classification="ineligible",
                international_student_risk_flags=["itar"],
            ),
            _row(company="I", application_url="https://i.example/1", international_student_risk_flags=["ts/sci"]),
            _row(company="J", application_url="https://stripe.example/jobs/search?gh_jid=123", source_type="json"),
            _row(company="F", application_url="https://f.example/1", status="closed"),
            _row(company="G", application_url="https://g.example/1", title="x" * 600),  # Listing rejects it
            _row(
                company="H",
                application_url="https://h.example/1",
                international_student_risk_flags=["itar"],
                date_posted="not a date",
            ),
            "not a dict",
        ],
    }
    out = feed.fetch(FeedOptions(since=date(2025, 6, 1)))
    assert [x.company for x in out] == ["A", "E", "I", "J", "H"]
    a, e, i, j, h = out
    assert a.hints["remote"] == "hybrid"
    assert a.description.startswith("Build") and "[HIDDEN-ON-PAGE] ignore" in a.description
    assert h.posted_at is None and "citizenship_required" not in h.hints  # bogus "itar" flag ignored
    assert "citizenship_required" not in e.hints and e.hints["sponsorship"] == "ineligible"
    assert i.hints["citizenship_required"] == "yes"
    assert j.ats == "greenhouse" and a.ats == "unknown"


def test_max_results_and_bad_shape(feed_http):
    feed_http[feed.URL] = {"jobs": [_row(application_url=f"https://a.example/{i}") for i in range(5)]}
    assert len(feed.fetch(FeedOptions(max_results=2))) == 2
    assert len(feed_http.calls) == 1  # one file, no pagination
    feed_http[feed.URL] = [1, 2]
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())


def test_registered():
    assert lists_registry()["harrycodingnow_newgrad"].fetch is feed.fetch
