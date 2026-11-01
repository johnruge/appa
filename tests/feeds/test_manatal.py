"""Manatal career pages, from a recorded slice of a real Career Page API response."""

from __future__ import annotations

import json

import httpx
import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import manatal as feed

SETTINGS = "https://api.manatal.com/open/v3/career-page/manatal/"
JOBS = "https://api.manatal.com/open/v3/career-page/manatal/jobs/"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _page(results, more=False):
    """A DRF page body."""
    return {
        "count": 99,
        "next": "https://core.api.manatal.com/x/?page=9" if more else None,
        "previous": None,
        "results": results,
    }


def test_reads_jobs_with_hints(feed_http, load_fixture):
    feed_http[SETTINGS] = {"name": "Manatal Co LTD", "is_organizations_visible": True}
    page = json.loads(load_fixture("manatal.json"))
    page["next"] = None
    feed_http[JOBS] = page
    out = feed.fetch("manatal")
    assert len(out) == 4
    assert str(feed_http.calls[1].url) == JOBS + "?page=1&page_size=100"
    mk = out[0]
    assert (mk.company, mk.title) == ("Manatal Co LTD", "AI Content Marketing Internship")
    assert mk.url == "https://www.careers-page.com/manatal/job/L9Y69V66"
    assert mk.ats == "manatal" and mk.ats_job_id == "2883259" and mk.source_ref == "L9Y69V66"
    assert mk.location == "Bangkok, Bangkok, Thailand" and mk.posted_at is None
    assert mk.hints == {"country": "Thailand", "employment_type": "full_time", "category": "Marketing"}
    assert "Manatal" in mk.description and "<p" not in mk.description
    cs = out[1]
    assert cs.hints["remote"] == "yes" and cs.location == "Remote (Bangkok, Bangkok, Thailand)"
    assert out[2].location == "" and "country" not in out[2].hints and out[2].hints["category"] == "Engineering"
    assert out[3].title == "Lead Software Engineer (Full Stack)" and out[3].ats_job_id == "1972344"
    assert all("/api" not in x.url and "manatal.com" not in x.url for x in out)


def test_company_falls_back_to_slug(feed_http, load_fixture):
    feed_http[SETTINGS] = httpx.Response(500)
    feed_http[JOBS] = _page(json.loads(load_fixture("manatal.json"))["results"][:1])
    assert feed.fetch("manatal")[0].company == "manatal"


def test_pagination_stops_and_caps(feed_http, load_fixture):
    rows = json.loads(load_fixture("manatal.json"))["results"]
    feed_http[SETTINGS] = {"name": "Manatal Co LTD"}
    feed_http[JOBS + "?page=1"] = _page(rows[:2], more=True)
    feed_http[JOBS + "?page=2"] = _page(rows[2:], more=False)
    assert len(feed.fetch("manatal")) == 4
    assert [str(c.url) for c in feed_http.calls[1:3]] == [
        JOBS + "?page=1&page_size=100",
        JOBS + "?page=2&page_size=100",
    ]
    assert len(feed_http.calls) == 3
    feed_http.calls.clear()
    feed_http[JOBS + "?page=2"] = _page([], more=True)  # empty page stops even with next
    assert len(feed.fetch("manatal")) == 2 and len(feed_http.calls) == 3
    feed_http.calls.clear()
    feed_http[JOBS + "?page=2"] = _page(rows[2:], more=True)
    feed_http[JOBS + "?page=3"] = _page(rows, more=True)  # all repeats: deduped
    feed_http[JOBS + "?page=4"] = _page([])
    assert len(feed.fetch("manatal")) == 4
    assert len(feed.fetch("manatal", FeedOptions(max_results=3))) == 3


def test_page_cap(feed_http, load_fixture, monkeypatch):
    rows = json.loads(load_fixture("manatal.json"))["results"]
    monkeypatch.setattr(feed, "MAX_PAGES", 3)
    feed_http[SETTINGS] = {"name": "M"}
    feed_http[JOBS] = _page(rows, more=True)  # next never null
    feed.fetch("manatal")
    assert len(feed_http.calls) == 1 + 3


def test_malformed_rows_skipped(feed_http, load_fixture):
    rows = json.loads(load_fixture("manatal.json"))["results"]
    feed_http[SETTINGS] = {"name": "M"}
    bad = [
        {"id": 1, "hash": "AB12", "position_name": ""},
        {"id": 2, "hash": "../x", "position_name": "SWE"},
        {"id": "x", "hash": "AB12", "position_name": "SWE"},
        {"hash": "AB13", "position_name": "SWE"},
        "not a job",
        None,
    ]
    feed_http[JOBS] = _page(bad + rows[:2])
    assert [x.ats_job_id for x in feed.fetch("manatal")] == ["2883259", "1217144"]
    feed_http[JOBS] = _page([])
    assert feed.fetch("manatal") == []


def test_http_errors_raise(feed_http):
    feed_http[SETTINGS] = {"name": "M"}
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchboard")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://www.careers-page.com/manatal/job/L9Y69V66", "manatal"),
        ("https://careers-page.com/manatal", "manatal"),
        ("https://www.careers-page.com/Acme-Co_2/", "Acme-Co_2"),
        ("https://api.manatal.com/open/v3/career-page/manatal/jobs/", "manatal"),
        ("https://core.api.manatal.com/open/v3/career-page/manatal/jobs/?page=2", "manatal"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilcareers-page.com/manatal",
        "https://www.careers-page.com.evil.com/manatal",
        "https://careers-page.com/",
        "https://www.careers-page.com/static/app.js",
        "https://manatal.careers-page.com/",
        "https://api.manatal.com/open/v3/jobs/",
        "https://www.manatal.com/careers",
        "https://evilmanatal.com/open/v3/career-page/x/",
        "https://api.manatal.com.evil.com/open/v3/career-page/x/",
        "https://www.careers-page.com/a.b/job/x",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a" * 101, "a b", "a?b=1"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
