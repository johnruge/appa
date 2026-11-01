"""Rippling boards, from a recorded slice of real board API responses."""

from __future__ import annotations

import json

import httpx
import pytest

from appa.feeds import FeedOptions, Listing
from appa.feeds.boards import rippling as feed

LIST = "https://ats.rippling.com/api/v2/board/rippling/jobs"
DETAIL = LIST + "/84d388b6-7656-434c-8862-0312eb6b97ac"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Paging pauses are skipped in tests."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _routes(feed_http, load_fixture):
    """Serve the recorded list page and posting detail."""
    feed_http[LIST + "?"] = load_fixture("rippling.json")
    feed_http[DETAIL] = load_fixture("rippling_job.json")


def test_reads_board_and_merges_locations(feed_http, load_fixture):
    _routes(feed_http, load_fixture)
    out = feed.fetch("rippling")
    assert [x.ats_job_id for x in out] == [
        "84d388b6-7656-434c-8862-0312eb6b97ac",
        "13bf8b65-fb2d-4740-b0a4-34046e5529cc",
        "eb23a6c5-2260-4fb7-b1a5-7e4d23c4417d",
        "dce595ba-1aea-41ce-af86-377d1c36888e",
    ]
    first = feed_http.calls[0]
    assert first.url.params["page"] == "0" and first.url.params["pageSize"] == "100"
    bo = out[0]
    assert (bo.company, bo.title, bo.ats) == ("Rippling", "Business Operations Manager", "rippling")
    assert bo.url == "https://ats.rippling.com/rippling/jobs/84d388b6-7656-434c-8862-0312eb6b97ac"
    assert bo.location == "San Francisco, CA; New York, NY"  # two rows merged into one job
    assert bo.hints == {"remote": "no", "country": "United States", "category": "Bizops"}
    assert bo.description == "" and bo.posted_at is None
    hr = [x for x in out if x.title.startswith("HR Consultant")][0]
    assert hr.location == "London, United Kingdom; Dublin, Ireland"
    assert hr.hints["country"] == "United Kingdom, Ireland"
    assert all(x.url.startswith("https://ats.rippling.com/rippling/jobs/") and "/api/" not in x.url for x in out)


def test_describe_reads_detail(feed_http, load_fixture):
    _routes(feed_http, load_fixture)
    x = feed.fetch("rippling")[0]
    text = feed.describe(x)
    assert "About Rippling" in text and "<p" not in text and "<span" not in text
    assert "Pay range (US): 185000-240000 USD/YEAR" in text
    assert str(feed_http.calls[-1].url) == DETAIL
    with pytest.raises(ValueError):
        feed.describe(Listing(company="X", title="Y", url="https://evil.com/rippling/jobs/x"))


def test_paging_stops_and_company_fallback(feed_http, load_fixture):
    items = json.loads(load_fixture("rippling.json"))["items"]
    feed_http[LIST + "?page=0"] = {"items": items[:3], "page": 0, "pageSize": 100, "totalItems": 9, "totalPages": 3}
    feed_http[LIST + "?page=1"] = {"items": items[3:], "page": 1, "pageSize": 100, "totalItems": 9, "totalPages": 3}
    feed_http[LIST + "?page=2"] = {"items": [], "page": 2, "pageSize": 100, "totalItems": 9, "totalPages": 3}
    feed_http[DETAIL] = httpx.Response(500)  # name falls back to the slug
    out = feed.fetch("rippling")
    pages = [c.url.params["page"] for c in feed_http.calls if "page" in c.url.params]
    assert pages == ["0", "1", "2"] and len(out) == 4 and out[0].company == "Rippling"
    feed_http[LIST + "?page=0"] = {"items": [], "page": 0, "pageSize": 100, "totalItems": 0, "totalPages": 0}
    assert feed.fetch("rippling") == []


def test_cap_and_malformed_rows(feed_http, load_fixture):
    _routes(feed_http, load_fixture)
    items = json.loads(load_fixture("rippling.json"))["items"]
    bad = [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "SWE", "url": "javascript:alert(1)"},
        {"id": "22222222-2222-2222-2222-222222222222", "name": "", "url": "https://ats.rippling.com/rippling/jobs/2"},
        {"id": "33333333-3333-3333-3333-333333333333", "name": "SWE", "url": "https://evil.com/rippling/jobs/3"},
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "name": "SWE Remote",
            "department": {"name": "-"},
            "url": "https://ats.rippling.com/rippling/jobs/44444444-4444-4444-4444-444444444444",
            "locations": [{"name": "Remote (US)", "country": "United States", "workplaceType": "REMOTE"}],
        },
        "not a row",
        {"name": "no id"},
    ]
    feed_http[LIST + "?"] = {"items": items + bad, "page": 0, "pageSize": 100, "totalItems": 12, "totalPages": 1}
    out = feed.fetch("rippling")
    assert len(out) == 5 and out[-1].hints == {"remote": "yes", "country": "United States"}
    assert len(feed.fetch("rippling", FeedOptions(max_results=2))) == 2


def test_http_errors_raise(feed_http):
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchboard")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://ats.rippling.com/boom-supersonic/jobs", "boom-supersonic"),
        ("https://ats.rippling.com/rippling/jobs/84d388b6-7656-434c-8862-0312eb6b97ac", "rippling"),
        ("https://ATS.rippling.com/purefacts_jobs/jobs/139cf84b-50cd-48b6-8202-bc2a6a55abe2?src=x", "purefacts_jobs"),
        ("https://ats.rippling.com/en-GB/boom-supersonic/jobs", "boom-supersonic"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilrippling.com/x/jobs",
        "https://ats.rippling.com.evil.com/x/jobs",
        "https://rippling.com/x/jobs",
        "https://www.rippling.com/careers/open-roles",
        "https://app.rippling.com/x/jobs",
        "https://ats.rippling.com/",
        "https://ats.rippling.com/boom-supersonic",
        "https://ats.rippling.com/api/v2/board/x/jobs",
        "https://ats.rippling.com/internal/jobs",
        "https://ats.rippling.com/a.b/jobs",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a" * 101, "a b", "x?y", "a.b"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
