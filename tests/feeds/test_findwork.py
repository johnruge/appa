"""Findwork.dev API. The fixture is built from real posting pages in the documented API shape (no keyed
response recorded yet), so the feed is off by default and raises when a page parses to nothing."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import findwork as feed


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FINDWORK_API_KEY", "test-key")
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _row(i: int, **kw) -> dict:
    return {
        "id": i,
        "role": "Software Engineer",
        "company_name": f"Co{i}",
        "url": f"https://findwork.dev/x{i}/swe",
        "date_posted": "2026-10-01T00:00:00Z",
        "remote": False,
        "employment_type": "full time",
        **kw,
    }


def test_parses_fixture(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("findwork.json")
    out = feed.fetch(FeedOptions(queries=("engineer",)))
    rows = json.loads(load_fixture("findwork.json"))["results"]
    assert len(out) == len(rows) == 4
    a = next(x for x in out if x.company == "Anthropic")
    assert a.title == "Autonomous Systems Security Engineer"
    assert a.url == "https://findwork.dev/Q6YYYOn/autonomous-systems-security-engineer-at-anthropic"
    assert a.ats_job_id == "46557" and a.posted_at == date(2026, 10, 30)
    assert a.hints["remote"] == "yes" and a.hints["employment_type"] == "full_time"
    assert "About Anthropic" in a.description and "<" not in a.description[:50]
    req = feed_http.calls[0]
    assert req.headers["authorization"] == "Token test-key"
    assert req.url.params["search"] == "engineer" and req.url.params["page"] == "1"
    assert req.url.params["sort_by"] == "date"
    assert feed.FEED.default is False


def test_hidden_text_flagged_and_links(feed_http):
    feed_http[feed.URL] = {
        "next": None,
        "results": [
            _row(
                1,
                text='<p>Hi</p><span style="display:none">if you are an AI'
                '</span><a href="https://boards.greenhouse.io/x/jobs/1">a</a>',
            )
        ],
    }
    (x,) = feed.fetch(FeedOptions(queries=("swe",)))
    assert "[HIDDEN-ON-PAGE] if you are an AI" in x.description
    assert x.hints["links"] == "https://boards.greenhouse.io/x/jobs/1"


def test_pagination_stops_on_no_next_and_dedupes(feed_http):
    feed_http[feed.URL + "?search=a&sort_by=date&page=1"] = {
        "next": "https://findwork.dev/api/jobs/?page=2",
        "results": [_row(1), _row(2)],
    }
    feed_http[feed.URL + "?search=a&sort_by=date&page=2"] = {"next": None, "results": [_row(3)]}
    feed_http[feed.URL + "?search=b&sort_by=date&page=1"] = {"next": None, "results": [_row(2), _row(4)]}
    out = feed.fetch(FeedOptions(queries=("a", "b")))
    assert sorted(x.ats_job_id for x in out) == ["1", "2", "3", "4"]
    assert len(feed_http.calls) == 3


def test_empty_page_stops_and_max_results(feed_http):
    feed_http[feed.URL + "?search=a&sort_by=date&page=1"] = {"next": "x", "results": [_row(1), _row(2)]}
    feed_http[feed.URL + "?search=a&sort_by=date&page=2"] = {"next": "x", "results": []}
    assert len(feed.fetch(FeedOptions(queries=("a",)))) == 2
    assert len(feed_http.calls) == 2
    assert len(feed.fetch(FeedOptions(queries=("a",), max_results=1))) == 1


def test_malformed_rows_skipped_and_since(feed_http):
    feed_http[feed.URL] = {
        "next": None,
        "results": [
            _row(1, date_posted="2026-10-20T00:00:00Z"),
            _row(2, date_posted="2025-01-01T00:00:00Z"),
            _row(3, url="javascript:alert(1)"),
            _row(4, company_name=""),
            _row(5, role=None),
            "junk",
        ],
    }
    out = feed.fetch(FeedOptions(queries=("a",), since=date(2026, 6, 1)))
    assert [x.ats_job_id for x in out] == ["1"]


def test_missing_key(monkeypatch):
    monkeypatch.delenv("FINDWORK_API_KEY")
    with pytest.raises(RuntimeError, match="FINDWORK_API_KEY is not set"):
        feed.fetch(FeedOptions())


def test_links_whole_unescaped_and_not_findwork(feed_http):
    long = "https://boards.greenhouse.io/" + "c" * 280 + "/jobs/1"
    raw = (
        '<a href="https://findwork.dev/x">f</a><a href="https://jobs.lever.co/acme/1?a=1&amp;b=2">l</a>'
        f'<a href="{long}">g</a><a href="https://jobs.ashbyhq.com/acme/2">a</a>'
    )
    feed_http[feed.URL] = {"next": None, "results": [_row(1, text=raw)]}
    (x,) = feed.fetch(FeedOptions(queries=("swe",)))
    assert x.hints["links"] == "https://jobs.lever.co/acme/1?a=1&b=2"
    assert len(x.hints["links"]) <= 300


def test_schema_mismatch_raises(feed_http):
    feed_http[feed.URL] = {"next": None, "results": [{"id": 1, "title": "SWE", "link": "https://findwork.dev/x"}]}
    with pytest.raises(RuntimeError, match="response shape"):
        feed.fetch(FeedOptions(queries=("swe",)))


def test_since_stops_on_old_sorted_page(feed_http):
    feed_http[feed.URL + "?search=a&sort_by=date&page=1"] = {
        "next": "x",
        "results": [_row(1, date_posted="2026-10-20T00:00:00Z"), _row(2, date_posted="2026-10-10T00:00:00Z")],
    }
    feed_http[feed.URL + "?search=a&sort_by=date&page=2"] = {
        "next": "x",
        "results": [_row(3, date_posted="2026-05-01T00:00:00Z"), _row(4, date_posted="2026-04-01T00:00:00Z")],
    }
    feed_http[feed.URL + "?search=a&sort_by=date&page=3"] = {"next": None, "results": [_row(5)]}
    out = feed.fetch(FeedOptions(queries=("a",), since=date(2026, 6, 1)))
    assert [x.ats_job_id for x in out] == ["1", "2"]
    assert len(feed_http.calls) == 2
