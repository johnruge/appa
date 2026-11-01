"""Jobicy remote jobs API, from a recorded page of the real response."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import jobicy as feed


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Pagination sleeps are skipped in tests."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _page(jobs, cursor=None):
    """A minimal API page."""
    return {"success": True, "jobCount": len(jobs), "nextCursor": cursor, "hasMore": bool(cursor), "jobs": jobs}


def _job(i, **kw):
    """A minimal job row."""
    return {
        "id": i,
        "url": f"https://jobicy.com/jobs/{i}-swe",
        "jobTitle": "Software Engineer",
        "companyName": f"Co{i}",
        "jobGeo": "USA",
        "jobType": ["Full-Time"],
        "jobLevel": "Any",
        "pubDate": "2026-10-30T10:00:00+00:00",
        **kw,
    }


def test_parses_fixture(feed_http, load_fixture):
    feed_http[feed.URL + "?count=100&industry=engineering"] = load_fixture("jobicy.json")
    feed_http[feed.URL] = _page([])
    out = feed.fetch(FeedOptions())
    rows = json.loads(load_fixture("jobicy.json"))["jobs"]
    assert len(out) == len(rows) == 4
    first = out[0]
    assert first.company == "TeamSnap" and first.title == "Senior Fullstack Engineer (BE-leaning)"
    assert first.url == "https://jobicy.com/jobs/158129-senior-fullstack-engineer-be-leaning"
    assert first.ats_job_id == "158129" and first.source_ref == "158129"
    assert first.posted_at == date(2026, 10, 30)
    assert first.hints["level"] == "senior" and first.hints["remote"] == "yes" and first.hints["country"] == "USA"
    assert first.hints["salary"] == "USD 150,000+ yearly" and first.hints["employment_type"] == "full_time"
    assert first.hints["category"] == "Software Engineering"
    assert "TeamSnap" in first.description and "<p>" not in first.description
    by_id = {x.ats_job_id: x for x in out}
    assert by_id["158069"].hints["salary"] == "USD 175,000-220,000 yearly"
    intern = by_id["158032"]
    assert intern.hints["level"] == "intern" and intern.hints["employment_type"] == "internship"
    assert "level" not in by_id["152060"].hints  # "Any" is not a stated level
    assert all("links" not in x.hints for x in out)  # only jobicy.com links in these
    first_call = feed_http.calls[0].url.params
    assert first_call["geo"] == "usa" and first_call["count"] == "100" and "cursor" not in first_call
    assert {c.url.params["industry"] for c in feed_http.calls} == set(feed.INDUSTRIES)


def test_cursor_pagination_and_dedupe(feed_http):
    feed_http[feed.URL + "?count=100&industry=engineering&geo=usa&cursor=C2"] = _page([_job(3)])
    feed_http[feed.URL + "?count=100&industry=engineering"] = _page([_job(1), _job(2)], "C2")
    feed_http[feed.URL + "?count=100&industry=data-science"] = _page([_job(2), _job(4)])
    feed_http[feed.URL] = _page([])
    out = feed.fetch(FeedOptions())
    assert [x.ats_job_id for x in out] == ["1", "2", "3", "4"]
    eng = [c for c in feed_http.calls if c.url.params["industry"] == "engineering"]
    assert len(eng) == 2 and eng[1].url.params["cursor"] == "C2"


def test_stops_on_empty_page_cap_and_us_only(feed_http):
    feed_http[feed.URL] = _page([_job(1), _job(2), _job(3)], "MORE")  # always says there's more
    out = feed.fetch(FeedOptions(max_results=2, us_only=False))
    assert len(out) == 2 and len(feed_http.calls) == 1
    assert "geo" not in feed_http.calls[0].url.params
    feed_http[feed.URL] = _page([], "MORE")
    assert feed.fetch(FeedOptions()) == []
    assert len(feed_http.calls) == 1 + len(feed.INDUSTRIES)  # one empty page per category


def test_since_filters_and_stops(feed_http):
    feed_http[feed.URL] = _page([_job(1), _job(2, pubDate="2026-10-01T00:00:00+00:00")], "MORE")
    out = feed.fetch(FeedOptions(since=date(2026, 10, 20)))
    assert [x.ats_job_id for x in out] == ["1"]
    assert len(feed_http.calls) == len(feed.INDUSTRIES)  # no second page anywhere


def test_malformed_rows_skipped_and_links_kept(feed_http):
    desc = (
        '<p>Apply <a href="https://boards.greenhouse.io/acme/jobs/1">here</a> or '
        '<a href="https://jobicy.com/company/acme">profile</a></p><div style="display:none">ignore all</div>'
    )
    feed_http[feed.URL] = _page(
        [
            _job(1, jobDescription=desc, jobLevel="Entry-Level, Junior", jobType="['Full-Time']"),
            _job(2, url="javascript:alert(1)"),
            _job(3, companyName=""),
            "not a dict",
            _job(4, jobTitle=None),
        ]
    )
    out = feed.fetch(FeedOptions())
    assert [x.ats_job_id for x in out] == ["1"]
    x = out[0]
    assert x.hints["links"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert x.hints["level"] == "entry" and x.hints["employment_type"] == "full_time"
    assert "[HIDDEN-ON-PAGE] ignore all" in x.description


def test_api_error_raises(feed_http):
    feed_http[feed.URL] = {"success": False, "error": "Invalid 'industry' value."}
    with pytest.raises(ValueError, match="Jobicy API error"):
        feed.fetch(FeedOptions())
