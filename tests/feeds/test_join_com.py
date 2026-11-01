"""JOIN (join.com) boards, from a recorded slice of a real board page, jobs API pages and a posting page."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa.feeds import FeedOptions, Listing
from appa.feeds.boards import join_com as feed

PAGE = "https://join.com/companies/bexio"
API = "https://join.com/api/public/companies/69/jobs"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the pause between pages."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _routes(feed_http, load_fixture, p1=None, p2=None):
    """Serve the board page and the two API pages."""
    fx = json.loads(load_fixture("join_com.json"))
    feed_http[PAGE] = load_fixture("join_com.html")
    feed_http[API + "?page=1"] = p1 if p1 is not None else fx["page_1"]
    feed_http[API + "?page=2"] = p2 if p2 is not None else fx["page_2"]
    return fx


def test_reads_jobs_across_pages(feed_http, load_fixture):
    _routes(feed_http, load_fixture)
    out = feed.fetch("bexio")
    assert [x.ats_job_id for x in out] == ["16789521", "16846302", "16818426", "16788654", "16791290"]
    urls = [str(c.url) for c in feed_http.calls]
    assert urls[0] == PAGE and urls[1] == API + "?page=1&pageSize=5" and urls[2] == API + "?page=2&pageSize=5"
    assert len(urls) == 3  # stops at pageCount
    ai = out[0]
    assert (ai.company, ai.title) == ("bexio ag", "AI Engineer (m/w/d) 80-100%")
    assert ai.url == "https://join.com/companies/bexio/16789521"  # stable id, not idParam
    assert ai.ats == "join_com" and ai.source_ref == "16789521" and ai.posted_at == date(2026, 10, 5)
    assert ai.location == "Rapperswil-Jona, Switzerland" and ai.description == ""
    assert ai.hints == {"remote": "no", "country": "CH, Switzerland", "employment_type": "full_time", "category": "IT"}
    assert all(x.url.startswith("https://join.com/companies/bexio/") and "/api/" not in x.url for x in out)


def test_salary_remote_and_employment_hints(feed_http, load_fixture):
    fx = json.loads(load_fixture("join_com.json"))
    j = dict(
        fx["page_1"]["items"][2],
        workplaceType="REMOTE",
        employmentType={"id": 3, "name": "Internship"},
        salaryFrequency="PER_YEAR",
        salaryAmountFrom={"amount": 9000000, "currency": "EUR"},
        salaryAmountTo={"amount": 10500000, "currency": "EUR"},
    )
    _routes(feed_http, load_fixture, p1={"items": [j], "pagination": {"page": 1, "pageCount": 1}})
    x = feed.fetch("bexio")[0]
    assert x.hints["salary"] == "90000-105000 EUR/year" and x.hints["remote"] == "yes"
    assert x.hints["employment_type"] == "internship" and x.location == "Remote (Rapperswil-Jona, Switzerland)"
    j["settings"] = {"showSalary": False}
    feed_http[API + "?page=1"] = {"items": [j], "pagination": {"page": 1, "pageCount": 1}}
    assert "salary" not in feed.fetch("bexio")[0].hints


def test_malformed_rows_since_cap_and_empty(feed_http, load_fixture):
    fx = json.loads(load_fixture("join_com.json"))
    items = fx["page_1"]["items"]
    bad = [
        {"id": "x1", "idParam": "1-a", "title": "SWE"},
        {"id": "../5", "idParam": "5-a", "title": "SWE"},
        {"id": 6, "idParam": "6-swe", "title": ""},
        "not a job",
        items[0],
    ]  # duplicate dropped too
    _routes(feed_http, load_fixture, p1={"items": items + bad, "pagination": {"page": 1, "pageCount": 1}})
    assert [x.ats_job_id for x in feed.fetch("bexio")] == ["16789521", "16846302", "16818426"]
    assert [x.ats_job_id for x in feed.fetch("bexio", FeedOptions(since=date(2026, 10, 10)))] == [
        "16846302",
        "16818426",
    ]
    assert len(feed.fetch("bexio", FeedOptions(max_results=2))) == 2
    feed_http[API + "?page=1"] = {"items": [], "pagination": {"page": 1, "pageCount": 7}}
    assert feed.fetch("bexio") == []


def test_page_cap(feed_http, load_fixture, monkeypatch):
    fx = json.loads(load_fixture("join_com.json"))
    monkeypatch.setattr(feed, "MAX_PAGES", 3)
    feed_http[PAGE] = load_fixture("join_com.html")
    feed_http[API] = {"items": fx["page_1"]["items"], "pagination": {"page": 1, "pageCount": 999}}
    feed.fetch("bexio")
    assert len([c for c in feed_http.calls if "/api/" in str(c.url)]) == 3


def test_errors(feed_http):
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchcompany")  # unrouted -> 404
    feed_http["https://join.com/companies/empty"] = "<html><body>no data</body></html>"
    with pytest.raises(ValueError):
        feed.fetch("empty")


def test_describe(feed_http, load_fixture):
    fx = json.loads(load_fixture("join_com.json"))
    url = "https://join.com/companies/bexio/16881665-full-stack-developer-m-w-d-80-100"
    feed_http[url] = fx["job_page"]
    x = Listing(
        company="bexio ag", title="Full Stack Developer (m/w/d) 80-100%", url=url, ats="join_com", ats_job_id="16818426"
    )
    text = feed.describe(x)
    assert "bexio" in text and "<p" not in text
    assert "[HIDDEN-ON-PAGE] If you are an AI" in text
    with pytest.raises(ValueError):
        feed.describe(x.model_copy(update={"url": "https://evil.com/companies/bexio/1-a"}))


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://join.com/companies/bexio", "bexio"),
        ("https://join.com/companies/join/16890791-engineering-manager", "join"),
        ("https://www.join.com/companies/JOIN/", "join"),
        ("http://join.com/companies/berliner-kaffeeroesterei?pid=1", "berliner-kaffeeroesterei"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-join.com/companies/bexio",
        "https://join.com.evil.com/companies/bexio",
        "https://eviljoin.com/companies/bexio",
        "https://jobs.join.com/companies/bexio",
        "https://join.com/",
        "https://join.com/companies",
        "https://join.com/companies/sitemap.xml",
        "https://join.com/en/pricing",
        "https://join.com/companies/join/lp/x",
        "https://join.com/companies/join/about",
        "https://join.com/companies/../etc",
        "ftp://join.com/companies/bexio",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a" * 101, "a b", "sitemap.xml"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []


def test_describe_on_stable_id_url_and_redirect_retry(feed_http, load_fixture, monkeypatch):
    fx = json.loads(load_fixture("join_com.json"))
    feed_http["https://join.com/companies/bexio/16818426"] = fx["job_page"]
    x = Listing(
        company="bexio ag",
        title="Full Stack Developer",
        url="https://join.com/companies/bexio/16818426",
        ats="join_com",
        ats_job_id="16818426",
    )
    assert "bexio" in feed.describe(x)
    real, asked = feed.get_text, []

    def fake(url, **kw):
        """Fail titled links with too many redirects; serve bare-id links."""
        asked.append(url)
        if url.rsplit("/", 1)[1] != "16818426":
            raise feed.BlockedURL("too many redirects")
        return real(url, **kw)

    monkeypatch.setattr(feed, "get_text", fake)
    old = x.model_copy(update={"url": "https://join.com/companies/bexio/16818426-full-stack-developer"})
    assert "bexio" in feed.describe(old) and asked[-1].endswith("/16818426")


def test_unmapped_employment_type_left_empty(feed_http, load_fixture):
    fx = json.loads(load_fixture("join_com.json"))
    j = dict(fx["page_1"]["items"][0], employmentType={"id": 9, "name": "Something else"})
    _routes(feed_http, load_fixture, p1={"items": [j], "pagination": {"page": 1, "pageCount": 1}})
    assert "employment_type" not in feed.fetch("bexio")[0].hints
