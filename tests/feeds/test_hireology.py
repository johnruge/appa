"""Hireology boards, from a recorded slice of a real careers API response (slug hireology2)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import hireology as feed

API = "https://api.hireology.com/v2/public/careers/hireology2"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the pause between pages."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _rows(load_fixture) -> list[dict]:
    """The recorded jobs."""
    return json.loads(load_fixture("hireology.json"))["data"]


def test_reads_jobs_with_hints(feed_http, load_fixture):
    feed_http[API] = load_fixture("hireology.json")
    out = feed.fetch("hireology2")
    assert len(out) == 5 and len(feed_http.calls) == 1
    assert dict(feed_http.calls[0].url.params) == {"page": "1", "page_size": "100"}
    d = out[0]
    assert (d.company, d.title) == ("Hireology", "Director of Healthcare Partnerships")
    assert d.url == "https://careers.hireology.com/hireology2/2895642/description"
    assert d.ats == "hireology" and d.ats_job_id == "2895642" and d.posted_at == date(2026, 10, 5)
    assert d.location == "Remote" and d.hints["remote"] == "yes" and d.hints["salary"] == "145000/year"
    assert d.hints["employment_type"] == "full_time" and d.hints["category"] == "General"
    assert "Healthcare Partnerships" in d.description and "<p" not in d.description
    cc = out[1]
    assert cc.location == "Chicago, IL" and "remote" not in cc.hints and cc.hints["salary"] == "55000/year"
    assert out[2].hints["salary"] == "130000-150000/year"
    assert out[4].hints["employment_type"] == "full_time" and out[4].posted_at == date(2026, 10, 20)
    assert all(x.url.startswith("https://careers.hireology.com/") and "api." not in x.url for x in out)


def test_pagination_stops_at_count(monkeypatch, load_fixture):
    rows = _rows(load_fixture)
    pages = {1: rows[:2], 2: rows[2:4], 3: rows[4:]}
    calls = []

    def fake(url, params=None, headers=None):
        calls.append(params["page"])
        return {"data": pages.get(params["page"], []), "count": 5, "page": params["page"], "page_size": 2}

    monkeypatch.setattr(feed, "get_json", fake)
    assert len(feed.fetch("hireology2")) == 5 and calls == [1, 2, 3]
    calls.clear()
    assert len(feed.fetch("hireology2", FeedOptions(max_results=3))) == 3 and calls == [1, 2]
    calls.clear()
    pages[2] = []  # an empty page ends the loop even if count says more
    assert len(feed.fetch("hireology2")) == 2 and calls == [1, 2]


def test_page_cap(monkeypatch):
    calls = []

    def endless(url, params=None, headers=None):
        calls.append(params["page"])
        return {
            "data": [{"id": params["page"], "name": "SWE", "status": "Open", "organization": {"name": "X"}}],
            "count": 10**9,
            "page_size": 1,
        }

    monkeypatch.setattr(feed, "get_json", endless)
    assert len(feed.fetch("x")) == feed.MAX_PAGES and len(calls) == feed.MAX_PAGES


def test_since_skips_and_malformed_rows(feed_http, load_fixture):
    rows = _rows(load_fixture)
    bad = [
        {"id": 1, "name": "Join our Talent Community!", "status": "Open"},
        {"id": 2, "name": "", "status": "Open"},
        {"id": 3, "name": "SWE", "status": "Closed"},
        {"id": None, "name": "SWE", "status": "Open"},
        {"id": 4, "name": "SWE", "status": "Open", "career_site_url": "javascript:alert(1)"},
        "not a job",
        rows[0],
    ]  # duplicate dropped too
    feed_http[API] = {"data": rows + bad, "count": len(rows) + len(bad), "page": 1, "page_size": 100}
    out = feed.fetch("hireology2")
    assert [x.ats_job_id for x in out] == ["2895642", "2897445", "2898800", "2908935", "2910044", "4"]
    assert out[-1].url == "https://careers.hireology.com/hireology2/4/description"  # bad link rebuilt safely
    since = feed.fetch("hireology2", FeedOptions(since=date(2026, 10, 15)))
    assert [x.ats_job_id for x in since] == ["2908935", "2910044", "4"]
    feed_http[API] = {"data": [], "count": 0, "page": 1, "page_size": 100}
    assert feed.fetch("hireology2") == []


def test_http_errors_raise(feed_http):
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchboard")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://careers.hireology.com/bigtime", "bigtime"),
        ("https://careers.hireology.com/bigtime/", "bigtime"),
        ("https://careers.hireology.com/hireology2/2895642/description", "hireology2"),
        ("http://CAREERS.hireology.com/nextpoint/2857197/application?src=x", "nextpoint"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilhireology.com/bigtime",
        "https://careers.hireology.com.evil.com/bigtime",
        "https://hireology.com/bigtime",
        "https://www.hireology.com/pricing",
        "https://api.hireology.com/v2/public/careers/x",
        "https://careers.hireology.com/",
        "https://careers.hireology.com/sitemaps/sitemap-1.xml",
        "https://careers.hireology.com/robots.txt",
        "https://careers.hireology.com/bigtime/about/us",
        "https://x.careers.hireology.com/bigtime",
        "ftp://careers.hireology.com/bigtime",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "..", "-a", "a" * 101, "a b", "a?b"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
