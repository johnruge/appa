"""Himalayas remote-jobs search API, from a recorded page of real results."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import himalayas as feed

EMPTY = {"jobs": [], "totalCount": 0}
NOW = 1793318400  # 2026-10-30 UTC, before every fixture expiryDate


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real sleeping and no key from the environment."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)
    monkeypatch.delenv(feed.KEY_ENV, raising=False)
    monkeypatch.setattr(feed, "_now", lambda: NOW)


def test_parses_fixture(feed_http, load_fixture):
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1"] = load_fixture("himalayas.json")
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2"] = EMPTY
    out = feed.fetch(FeedOptions(queries=("x",), us_only=False))
    rows = json.loads(load_fixture("himalayas.json"))["jobs"]
    assert len(out) == len(rows) == 5
    a = out[0]
    assert a.company == "Askable" and a.url.startswith("https://himalayas.app/companies/askable/jobs/")
    assert a.ats_job_id == "askable/software-engineer-ai-coding-tasks-contract"
    assert a.hints["country"] == "worldwide" and a.hints["employment_type"] == "contract"
    assert a.hints["level"] == "mid" and a.hints["salary"] == "100 USD/hourly" and a.hints["remote"] == "yes"
    assert a.posted_at == date(2026, 10, 17) and a.location == "Remote"
    entry = [x for x in out if x.hints.get("level") == "entry"]
    assert {x.company for x in entry} == {"Ascensus", "CSC"}
    asc = next(x for x in entry if x.company == "Ascensus")
    assert asc.hints["salary"] == "65000-150000 USD/annual" and asc.location == "Remote (United States)"
    assert asc.description and "<" not in asc.description[:200]
    assert all(x.hints["category"] == "Developer" for x in out)


def test_params_pagination_and_dedupe(feed_http, load_fixture):
    fx = load_fixture("himalayas.json")
    for q in ("a", "b"):
        feed_http[f"{feed.URL}?q={q}&sort=recent&page=1&country=US"] = fx
        feed_http[f"{feed.URL}?q={q}&sort=recent&page=2&country=US"] = EMPTY
    out = feed.fetch(FeedOptions(queries=("a", "b")))
    assert len(out) == 5  # same jobs from both queries, deduped
    assert len(feed_http.calls) == 4  # each query stops at its empty page
    assert feed_http.calls[0].url.params["country"] == "US"
    assert "authorization" not in feed_http.calls[0].headers


def test_page_cap_and_max_results(feed_http, load_fixture, monkeypatch):
    feed_http[feed.URL] = load_fixture("himalayas.json")  # every page returns the same jobs
    monkeypatch.setattr(feed, "MAX_PAGES", 3)
    out = feed.fetch(FeedOptions(queries=("x",), us_only=False))
    assert len(out) == 5 and len(feed_http.calls) == 3
    assert len(feed.fetch(FeedOptions(queries=("x",), us_only=False, max_results=2))) == 2


def test_since_stops_and_malformed_rows_skipped(feed_http, monkeypatch):
    monkeypatch.setenv(feed.KEY_ENV, "k")
    good = {
        "companyName": "A",
        "title": "SWE",
        "applicationLink": "https://himalayas.app/companies/a/jobs/swe",
        "guid": "https://himalayas.app/companies/a/jobs/swe",
        "pubDate": "1793383973",
        "locationRestrictions": ["Canada"],
        "seniority": ["Entry-level"],
        "employmentType": "Intern",
    }
    old = {
        **good,
        "title": "Old",
        "guid": "https://himalayas.app/companies/a/jobs/old",
        "applicationLink": "https://himalayas.app/companies/a/jobs/old",
        "pubDate": "1735689600",
        "locationRestrictions": [],
    }
    bad = [
        {"companyName": "", "title": "x", "applicationLink": "https://himalayas.app/x"},
        {"companyName": "B", "title": "x", "applicationLink": "javascript:alert(1)"},
        "junk",
    ]
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1"] = {"jobs": [good, old, *bad]}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2"] = {"jobs": [old]}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=3"] = {"jobs": [good]}
    out = feed.fetch(FeedOptions(queries=("x",), us_only=False, since=date(2026, 1, 1)))
    assert [x.title for x in out] == ["SWE"] and out[0].hints["level"] == "intern"
    assert len(feed_http.calls) == 2  # page 2 was all older than since
    assert feed_http.calls[0].headers["authorization"] == "Bearer k"


def test_us_only_drops_other_countries(feed_http):
    row = {
        "companyName": "A",
        "title": "SWE",
        "applicationLink": "https://himalayas.app/companies/a/jobs/swe",
        "guid": "https://himalayas.app/companies/a/jobs/swe",
        "locationRestrictions": ["Canada"],
    }
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1"] = {"jobs": [row]}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2"] = EMPTY
    assert feed.fetch(FeedOptions(queries=("x",))) == []


def test_job_id_only_from_himalayas_guids():
    assert feed._job_id("https://himalayas.app/companies/linear/jobs/senior-engineer") == "linear/senior-engineer"
    assert feed._job_id("https://evilhimalayas.app/companies/a/jobs/b") is None
    assert feed._job_id("https://himalayas.app/jobs/b") is None


def test_http_errors_raise(feed_http):
    feed_http[feed.URL] = httpx.Response(429, content=b"slow down")
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch(FeedOptions(queries=("x",)))


def _row(slug, countries, **kw):
    """A minimal search result for a job at company 'a'."""
    link = f"https://himalayas.app/companies/a/jobs/{slug}"
    return {
        "companyName": "A",
        "title": slug,
        "applicationLink": link,
        "guid": link,
        "locationRestrictions": countries,
        **kw,
    }


def test_long_country_list_with_us_is_kept(feed_http):
    many = [f"Country {i:02d}" for i in range(59)]
    many.insert(40, "United States")  # 60 countries, the US buried deep in the list
    other = [f"Country {i:02d}" for i in range(60)]  # 60 countries, no US
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1&country=US"] = {"jobs": [_row("wide", many), _row("nous", other)]}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2&country=US"] = EMPTY
    out = feed.fetch(FeedOptions(queries=("x",)))
    assert [x.title for x in out] == ["wide"]
    x = out[0]
    assert x.location == "Remote (United States + 59 more)"
    assert x.hints["country"].startswith("United States, Country 00") and x.hints["country"].endswith(" more")
    assert len(x.hints["country"]) <= 300
    assert feed._location(other) == "Remote (60 countries)"
    assert feed._listing(_row("nous", other), NOW).hints["country"].endswith(" more")


def test_expired_rows_dropped(feed_http):
    rows = [_row("live", [], expiryDate=str(NOW + 86400)), _row("gone", [], expiryDate=NOW - 1)]
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1"] = {"jobs": rows}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2"] = EMPTY
    assert [x.title for x in feed.fetch(FeedOptions(queries=("x",), us_only=False))] == ["live"]


def test_since_stops_even_with_skipped_rows(feed_http):
    old = "1735689600"  # 2025-01-01
    page = [_row("o1", [], pubDate=old), _row("ca", ["Canada"], pubDate=old), {"title": "broken"}]
    feed_http[f"{feed.URL}?q=x&sort=recent&page=1&country=US"] = {"jobs": page}
    feed_http[f"{feed.URL}?q=x&sort=recent&page=2&country=US"] = {"jobs": page}
    assert feed.fetch(FeedOptions(queries=("x",), since=date(2026, 1, 1))) == []
    assert len(feed_http.calls) == 1  # the non-US and malformed rows don't block the stop
