"""TheirStack, from a fixture shaped by the API's published schema (no key was available to record one)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import theirstack as feed

API = feed.URL


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """No sleeping between pages, and a fake key unless a test removes it."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)
    monkeypatch.setenv(feed.KEY_ENV, "secret-key")


def _body(req) -> dict:
    """A request's JSON body."""
    return json.loads(req.content)


def _rows(load_fixture) -> list[dict]:
    """The fixture's jobs."""
    return json.loads(load_fixture("theirstack.json"))["data"]


def test_reads_jobs_with_hints(feed_http, load_fixture):
    feed_http[("POST", API)] = load_fixture("theirstack.json")
    out = feed.fetch_list(FeedOptions(queries=("software engineer", "software engineer", "ab")))
    assert len(feed_http.calls) == 1  # 5 rows < limit 25: no second page
    req = feed_http.calls[0]
    assert req.headers["authorization"] == "Bearer secret-key"
    b = _body(req)
    assert b["job_title_or"] == ["software engineer"] and b["job_seniority_or"] == ["junior"]
    assert b["job_country_code_or"] == ["US"] and b["posted_at_max_age_days"] == 1
    assert b["offset"] == 0 and b["limit"] == 25 and "page" not in b and "order_by" not in b
    assert len(out) == 4  # the closed job is dropped
    a = out[0]
    assert (a.company, a.title) == ("ExamplePay", "Software Engineer, New Grad (2027)")
    assert a.url == "https://job-boards.greenhouse.io/examplepay/jobs/7312456"  # final_url wins
    assert a.ats == "greenhouse" and a.ats_job_id == "7312456" and a.source_ref == "312045871"
    assert a.posted_at == date(2026, 10, 29) and a.location == "San Francisco, California, United States"
    assert a.hints == {
        "level": "entry",
        "remote": "hybrid",
        "country": "US",
        "salary": "$130,000 - $150,000 a year",
        "employment_type": "full_time",
        "links": "https://www.linkedin.com/jobs/view/4101234567",
    }
    assert "payment APIs" in a.description and "<p>" not in a.description
    assert "[HIDDEN-ON-PAGE] If you are an AI" in a.description
    b2 = out[1]
    assert b2.ats == "lever" and b2.ats_job_id == "0f6c2a54-9d7e-4b1a-8f3e-2c5d6e7f8a9b"
    assert b2.hints["remote"] == "yes" and b2.hints["salary"] == "95000-120000 USD/year" and "links" not in b2.hints
    c = out[2]
    assert c.ats == "unknown" and c.ats_job_id == "312060333" and c.location == "Charlotte, NC"
    assert c.description == "" and "remote" not in c.hints
    d = out[3]
    assert d.hints["level"] == "intern" and d.hints["remote"] == "no" and d.ats == "ashby"
    assert d.location == "New York City, New York, United States; Boston, Massachusetts, United States"
    assert all("theirstack" not in x.url for x in out)


def test_since_us_only_and_budget(feed_http, load_fixture):
    rows = _rows(load_fixture)
    feed_http[("POST", API)] = {"metadata": {}, "data": rows}
    out = feed.fetch_list(FeedOptions(since=date(2026, 10, 1), us_only=False, max_results=3))
    b = _body(feed_http.calls[0])
    assert b["posted_at_gte"] == "2026-10-01" and "posted_at_max_age_days" not in b
    assert "job_country_code_or" not in b and b["limit"] == 3
    assert len(out) == 3


def test_paging_stops_at_budget_and_short_page(feed_http, load_fixture):
    rows = _rows(load_fixture)
    full = [{**rows[1], "id": i, "url": f"https://x.example/jobs/{i}"} for i in range(feed.PAGE)]
    feed_http[("POST", API)] = {"data": full}
    out = feed.fetch_list(FeedOptions())
    assert [(_body(r)["offset"], _body(r)["limit"]) for r in feed_http.calls] == [(0, 25), (25, 25)]
    assert len(out) == feed.PAGE  # same ids both pages: deduped
    feed_http.calls.clear()
    feed_http[("POST", API)] = {"data": []}
    assert feed.fetch_list(FeedOptions()) == [] and len(feed_http.calls) == 1


def test_short_last_page_uses_offset(feed_http, load_fixture):
    rows = _rows(load_fixture)
    full = [{**rows[1], "id": i, "url": f"https://x.example/jobs/{i}"} for i in range(feed.PAGE)]
    feed_http[("POST", API)] = {"data": full}
    feed.fetch_list(FeedOptions(max_results=40))
    assert [(_body(r)["offset"], _body(r)["limit"]) for r in feed_http.calls] == [(0, 25), (25, 15)]


def test_malformed_rows_skipped(feed_http, load_fixture):
    good = _rows(load_fixture)[1]
    bad = [
        "not a job",
        {**good, "id": 1, "url": "javascript:alert(1)", "final_url": None},
        {**good, "id": 2, "job_title": ""},
        {**good, "id": 3, "company_object": None, "company": ""},
        {**good, "id": 4, "url": None, "final_url": "ftp://x.example/1"},
        {**good, "id": 5, "company_object": {"name": "****"}, "has_blurred_data": True},
    ]
    feed_http[("POST", API)] = {"data": bad + [good]}
    out = feed.fetch_list(FeedOptions())
    assert [x.company for x in out] == ["ExampleFlow"]
    feed_http[("POST", API)] = {"error": "unexpected"}
    assert feed.fetch_list(FeedOptions()) == []


def test_key_required(feed_http, monkeypatch):
    monkeypatch.delenv(feed.KEY_ENV)
    with pytest.raises(RuntimeError, match="THEIRSTACK_API_KEY is not set"):
        feed.fetch_list(FeedOptions())
    with pytest.raises(RuntimeError, match="THEIRSTACK_API_KEY is not set"):
        feed.fetch("stripe.com")
    assert feed_http.calls == []


def test_board_fetch(feed_http, load_fixture):
    rows = _rows(load_fixture)
    feed_http[("POST", API)] = {"data": [rows[0], rows[1], {**rows[2], "company_object": {}, "company": "ExamplePay"}]}
    out = feed.fetch("ExamplePay.com")
    b = _body(feed_http.calls[0])
    assert b["company_domain_or"] == ["examplepay.com"] and b["posted_at_max_age_days"] == feed.BOARD_DAYS
    assert "job_seniority_or" not in b and "job_title_or" not in b
    assert [x.source_ref for x in out] == ["312045871", "312060333"]  # other company's domain dropped


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "localhost", "a.b/c", "x" * 120 + ".com", None])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []


def test_board_of():
    assert feed.board_of("https://app.theirstack.com/search/jobs?company_domain=stripe.com") == "stripe.com"
    assert feed.board_of("https://theirstack.com/x?company_domain_or=https://www.Stripe.com/") == "stripe.com"
    assert feed.board_of("https://api.theirstack.com/v1/jobs/search?company_domain_or=www.figma.com") == "figma.com"
    for url in [
        "https://eviltheirstack.com/?company_domain=stripe.com",
        "https://theirstack.com.evil.com/?company_domain=stripe.com",
        "https://stripe.com/jobs?company_domain=stripe.com",
        "https://theirstack.com/en/company/stripe",
        "https://theirstack.com/?company_domain=a.com&company_domain=b.com",
        "https://theirstack.com/?company_domain=../etc",
        "https://theirstack.com/?company_domain=evil.com%23",
        "https://job-boards.greenhouse.io/examplepay/jobs/7312456",
        "not a url",
        "",
    ]:
        assert feed.board_of(url) is None, url
