"""jobdataapi.com, from a recorded slice of a real anonymous /api/jobs/ response."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import jobdataapi as feed

API = feed.URL


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No sleeping between pages or retries, and no key unless a test sets one."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)
    monkeypatch.delenv(feed.KEY_ENV, raising=False)


def _q(req) -> dict:
    """A request's query parameters as a flat dict."""
    return {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}


def _rows(load_fixture) -> list[dict]:
    """The recorded results."""
    return json.loads(load_fixture("jobdataapi.json"))["results"]


def test_reads_results_with_hints(feed_http, load_fixture):
    feed_http[API] = load_fixture("jobdataapi.json")
    out = feed.fetch_list(FeedOptions(queries=("software engineer",)))
    assert len(out) == 5 and len(feed_http.calls) == 1  # anonymous: one page only
    assert _q(feed_http.calls[0]) == {"title": "software engineer", "country_code": "US"}
    assert "authorization" not in feed_http.calls[0].headers
    w = out[0]
    assert (w.company, w.title, w.location) == ("Walgreens", "Principal Software Engineer II", "DEERFIELD, IL")
    assert w.url == "https://jobs.walgreens.com/en/job/deerfield/principal-software-engineer-ii/1242/92464502752"
    assert w.ats_job_id == "92464502752" and w.source_ref == "61107857" and w.posted_at == date(2026, 10, 30)
    assert w.hints == {"employment_type": "full_time", "country": "US", "salary": "150000+ USD/year"}
    assert "Principal Software Engineer II" in w.description and "<p>" not in w.description
    assert out[1].hints["salary"] == "73800-106000 USD/year"
    assert out[2].ats_job_id == "JR-002101" and "level" not in out[2].hints  # MI is not passed on
    kla = out[3]
    assert kla.hints["level"] == "intern" and kla.hints["employment_type"] == "full_time, internship"
    assert out[4].company == "Google" and out[4].url.startswith("https://www.google.com/about/careers/")
    assert all("jobdataapi.com" not in x.url for x in out)


def test_queries_dedupe_since_and_malformed_rows(feed_http, load_fixture):
    rows = _rows(load_fixture)
    bad = [
        {"id": 1, "title": "SWE", "application_url": "javascript:alert(1)", "company": {"name": "X"}},
        {"id": 2, "title": "", "application_url": "https://x.example/1", "company": {"name": "X"}},
        {
            "id": 3,
            "title": "SWE",
            "application_url": "https://x.example/2",
            "company": {"name": "A", "is_agency": True},
        },
        {"id": 4, "title": "SWE", "application_url": "https://x.example/3", "company": "flat"},
        "not a job",
    ]
    old = {**rows[1], "id": 5, "ext_id": "old", "published": "2025-01-01T00:00:00Z"}
    feed_http[API] = {"count": 99, "next": f"{API}?page=2", "results": rows + bad + [old]}
    out = feed.fetch_list(FeedOptions(queries=("software engineer", "new grad", "ab"), since=date(2026, 1, 1)))
    assert len(feed_http.calls) == 2  # "ab" is too short to search
    assert len(out) == 5 and "old" not in {x.ats_job_id for x in out}  # deduped across both queries
    assert len(feed.fetch_list(FeedOptions(queries=("software",), max_results=2))) == 2
    feed_http[API] = {"count": 0, "next": None, "results": []}
    assert feed.fetch_list(FeedOptions()) == []


def test_anonymous_query_cap(feed_http):
    feed_http[API] = {"count": 0, "next": None, "results": []}
    feed.fetch_list(FeedOptions(queries=tuple(f"query {i}" for i in range(12)), us_only=False))
    assert len(feed_http.calls) == feed.ANON_QUERIES and "country_code" not in _q(feed_http.calls[0])


def test_keyed_paging_stops(feed_http, load_fixture, monkeypatch):
    monkeypatch.setenv(feed.KEY_ENV, "secret-key")
    rows = _rows(load_fixture)
    feed_http[f"{API}?title=software&country_code=US&page_size=1000&published_since=2026-10-01&page=2"] = {
        "count": 5,
        "next": None,
        "results": rows[3:],
    }
    feed_http[API] = {"count": 5, "next": f"{API}?page=2", "results": rows[:3]}
    out = feed.fetch_list(FeedOptions(queries=("software",), since=date(2026, 10, 1)))
    assert len(out) == 5 and len(feed_http.calls) == 2
    assert feed_http.calls[0].headers["authorization"] == "Api-Key secret-key"
    assert _q(feed_http.calls[1])["page"] == "2"
    feed_http[API] = {"count": 99, "next": f"{API}?page=2", "results": rows}  # never-ending pages are capped
    feed_http.calls.clear()
    feed.fetch_list(FeedOptions(queries=("software",)))
    assert len(feed_http.calls) == feed.MAX_PAGES


def test_retries_5xx_then_raises_4xx(feed_http, load_fixture):
    feed_http[API] = httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch_list(FeedOptions(queries=("software",)))
    assert len(feed_http.calls) == 3
    feed_http[API] = httpx.Response(429)
    feed_http.calls.clear()
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch_list(FeedOptions(queries=("software",)))
    assert len(feed_http.calls) == 1


def test_board_fetch_keeps_exact_company(feed_http, load_fixture):
    rows = _rows(load_fixture)
    other = {
        **rows[1],
        "id": 9,
        "ext_id": "9",
        "company": {**rows[1]["company"], "website_url": "https://notwalgreens.com"},
    }
    feed_http[API] = {"count": 2, "next": None, "results": [rows[0], other]}
    out = feed.fetch("Walgreens.com")
    assert [x.company for x in out] == ["Walgreens"]
    assert _q(feed_http.calls[0]) == {"company_website": "walgreens.com", "country_code": "US"}


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://jobdataapi.com/api/jobs/?company_website=walgreens.com", "walgreens.com"),
        ("https://www.jobdataapi.com/api/jobs/?company_website=https://www.google.com/&title=x", "google.com"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-jobdataapi.com/api/jobs/?company_website=walgreens.com",
        "https://jobdataapi.com.evil.com/api/jobs/?company_website=walgreens.com",
        "https://jobdataapi.com/docs/?company_website=walgreens.com",
        "https://jobdataapi.com/api/jobs/?title=software",
        "https://jobdataapi.com/api/jobs/?company_website=a.com&company_website=b.com",
        "https://jobdataapi.com/api/jobs/?company_website=walgreens.com|evil.com",
        "https://jobs.walgreens.com/en/job/deerfield/x/1242/92464502752",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize(
    "slug", ["../x", "a/b", "evil.com#", "", "walgreens", "a b.com", "a.com|b.com", "-a.com", "x" * 101 + ".com"]
)
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
