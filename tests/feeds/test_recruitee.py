"""Recruitee boards, from a recorded slice of real Careers Site API responses."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import recruitee as feed

API = "https://bunq.recruitee.com/api/offers/"


def test_reads_offers_with_hints(feed_http, load_fixture):
    feed_http[API] = load_fixture("recruitee.json")
    out = feed.fetch("bunq")
    assert len(out) == 4 and str(feed_http.calls[0].url) == API
    ds = out[0]
    assert (ds.company, ds.title) == ("bunq", "Data Scientist Marketing")
    assert ds.url == "https://careers.bunq.com/o/data-scientist-marketing-3"
    assert ds.ats == "recruitee" and ds.ats_job_id == "2800490" and ds.posted_at == date(2026, 10, 28)
    assert ds.location == "Amsterdam, Noord-Holland, Netherlands"
    assert ds.hints["remote"] == "hybrid" and ds.hints["country"] == "Netherlands" and "salary" not in ds.hints
    assert "XGBoost" in ds.description and "<p" not in ds.description  # requirements included, HTML gone
    ae = out[1]
    assert ae.hints["salary"] == "200000-250000 USD/year" and ae.hints["remote"] == "yes"
    assert ae.location.startswith("Remote (New York City") and ae.hints["employment_type"] == "full_time"
    assert out[2].hints["level"] == "mid" and out[2].hints["degrees"] == "bachelor degree"
    sdr = out[3]
    assert sdr.hints["level"] == "entry" and sdr.hints["country"] == "United States"
    assert "Chicago, Illinois, United States" in sdr.location
    assert all(x.url.startswith("https://") and "/api/" not in x.url for x in out)


def test_since_cap_and_malformed_rows(feed_http, load_fixture):
    offers = json.loads(load_fixture("recruitee.json"))["offers"]
    bad = [
        {"id": 1, "title": "SWE", "careers_url": "javascript:alert(1)", "company_name": "X"},
        {"id": 2, "title": "", "careers_url": "https://x.recruitee.com/o/a", "company_name": "X"},
        {"id": 3, "title": "SWE", "careers_url": "https://x.recruitee.com/o/b", "status": "closed"},
        "not an offer",
        offers[0],
    ]  # duplicate is dropped too
    feed_http[API] = {"offers": offers + bad}
    assert [x.ats_job_id for x in feed.fetch("bunq")] == ["2800490", "2694685", "2747829", "2803963"]
    assert [x.ats_job_id for x in feed.fetch("bunq", FeedOptions(since=date(2026, 10, 1)))] == [
        "2800490",
        "2747829",
        "2803963",
    ]
    assert len(feed.fetch("bunq", FeedOptions(max_results=2))) == 2
    feed_http[API] = {"offers": []}
    assert feed.fetch("bunq") == []


def test_http_errors_raise(feed_http):
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchboard")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://bunq.recruitee.com/o/data-scientist-marketing-3", "bunq"),
        ("https://Channable.recruitee.com/", "channable"),
        ("http://hostaway.recruitee.com/api/offers/", "hostaway"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilrecruitee.com/o/x",
        "https://bunq.recruitee.com.evil.com/o/x",
        "https://recruitee.com/",
        "https://www.recruitee.com/pricing",
        "https://app.recruitee.com/",
        "https://docs.recruitee.com/reference",
        "https://a.b.recruitee.com/",
        "https://careers.bunq.com/o/data-scientist-marketing-3",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a" * 64, "a b", "x.recruitee.com"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
