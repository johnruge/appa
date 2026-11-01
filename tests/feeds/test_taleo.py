"""Taleo career sections, from a recorded search page and posting page (HDR's external section)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import taleo

SLUG = "hdr:ex"
SEARCH_PAGE = "https://hdr.taleo.net/careersection/ex/jobsearch.ftl"
API = "https://hdr.taleo.net/careersection/rest/jobboard/searchjobs"
PAGE_HTML = (
    "<html><head><title>Job Search</title><title>Job Search | HDR</title></head><body><script>"
    "queryString: 'lang=en&amp;portal=101430233', portalNo: '101430233'</script></body></html>"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(taleo.time, "sleep", lambda s: None)


def _search(load_fixture, total: int = 5) -> dict:
    """The recorded search reply with its total set (the recording says 706)."""
    data = json.loads(load_fixture("taleo.json"))
    data["pagingData"]["totalCount"] = total
    return data


def _row(i: int) -> dict:
    return {
        "jobId": str(9000 + i),
        "contestNo": str(100000 + i),
        "linkedColumn": 0,
        "locationsColumns": [1],
        "column": [f"Software Engineer {i}", '["United States-Illinois-Chicago"]', "10/26/2026"],
    }


def test_parses_fixture_with_us_filter(feed_http, load_fixture):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = _search(load_fixture)
    out = taleo.fetch(SLUG, FeedOptions(queries=("software engineer",)))
    assert len(out) == 5
    first = out[0]
    assert first.company == "HDR"
    assert first.title == "Electrical Engineer - Battery Energy Storage System (BESS)"
    assert first.url == "https://hdr.taleo.net/careersection/ex/jobdetail.ftl?job=197089&lang=en"
    assert first.ats == "taleo" and first.ats_job_id == "197089" and first.source_ref == "792722"
    assert first.location == "Multiple Locations" and first.posted_at == date(2026, 10, 15)
    assert first.hints == {"country": "United States"}
    assert out[3].location == "United States-California-Los Angeles"
    assert out[2].title == "Control Systems Engineer"
    probe, page = (json.loads(c.content) for c in feed_http.calls[1:3])
    assert probe["filterSelectionParam"]["searchFilterSelections"] == []
    assert page["filterSelectionParam"]["searchFilterSelections"] == [
        {"id": "LOCATION", "selectedValues": ["1460483283"]}
    ]
    assert page["fieldData"]["fields"]["KEYWORD"] == "software engineer" and page["pageNo"] == 1
    req = feed_http.calls[2]
    assert req.url.params["portal"] == "101430233" and req.headers["tz"] == "GMT-04:00"
    assert len(feed_http.calls) == 3  # page, probe, one search page (total reached)


def test_no_filter_without_us_only(feed_http, load_fixture):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = _search(load_fixture)
    out = taleo.fetch(SLUG, FeedOptions(queries=("swe",), us_only=False))
    assert len(feed_http.calls) == 2
    assert json.loads(feed_http.calls[1].content)["filterSelectionParam"]["searchFilterSelections"] == []
    assert out[0].hints == {}


def test_since_filters_old_postings(feed_http, load_fixture):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = _search(load_fixture)
    out = taleo.fetch(SLUG, FeedOptions(queries=("swe",), us_only=False, since=date(2026, 10, 14)))
    assert [x.ats_job_id for x in out] == ["197089", "196958"]


def test_pagination_stops_and_caps(feed_http):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = {
        "requisitionList": [_row(i) for i in range(25)],
        "pagingData": {"currentPageNo": 1, "pageSize": 25, "totalCount": 500},
    }
    out = taleo.fetch(SLUG, FeedOptions(queries=("a", "b"), us_only=False))
    assert len(out) == 25 and out[0].posted_at == date(2026, 10, 26)
    assert [json.loads(c.content)["pageNo"] for c in feed_http.calls[1:]] == [1, 2, 1, 2]  # repeat page: stop
    out = taleo.fetch(SLUG, FeedOptions(queries=("a",), us_only=False, max_results=5))
    assert len(out) == 5


def test_stops_on_empty_page(feed_http):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = {"requisitionList": [], "pagingData": {"pageSize": 25, "totalCount": 500}}
    assert taleo.fetch(SLUG, FeedOptions(queries=("a",), us_only=False)) == []
    assert len(feed_http.calls) == 2


def test_malformed_rows_skipped(feed_http):
    feed_http[SEARCH_PAGE] = PAGE_HTML
    bad = [
        {"contestNo": "../x", "column": ["T", "[]", ""]},
        {"contestNo": "1", "column": ["", "[]", ""]},
        {"contestNo": "2", "column": "nope"},
        {"contestNo": "3", "column": ["T"], "linkedColumn": 5},
        "junk",
        {"contestNo": "4", "column": ["Ok", "not json", "garbage"], "linkedColumn": 0, "locationsColumns": [1]},
    ]
    feed_http[("POST", API)] = {"requisitionList": bad, "pagingData": {"pageSize": 25, "totalCount": 6}}
    out = taleo.fetch(SLUG, FeedOptions(queries=("a",), us_only=False))
    assert [(x.ats_job_id, x.location, x.posted_at) for x in out] == [("4", "not json", None)]


def test_sign_in_section_raises(feed_http):
    feed_http[SEARCH_PAGE] = "<html><title>Sign On</title><input name='portal' value='' /></html>"
    with pytest.raises(RuntimeError, match="no public job search"):
        taleo.fetch(SLUG)


def test_describe_reads_posting_page(feed_http, load_fixture):
    feed_http["https://hdr.taleo.net/careersection/ex/jobdetail.ftl"] = load_fixture("taleo_detail.html")
    feed_http[SEARCH_PAGE] = PAGE_HTML
    feed_http[("POST", API)] = _search(load_fixture)
    first = taleo.fetch(SLUG, FeedOptions(queries=("swe",), us_only=False))[0]
    text = taleo.describe(first)
    assert text.startswith("At HDR, our employee-owners")
    assert "Required Qualifications" in text and "comprehensive benefits package" in text
    assert "<span" not in text and "%3C" not in text and "United States-Ohio-Columbus" not in text
    assert feed_http.calls[-1].url.params["job"] == "197089"


def test_board_of():
    assert taleo.board_of("https://hdr.taleo.net/careersection/ex/jobsearch.ftl?lang=en") == "hdr:ex"
    assert taleo.board_of("https://hdr.taleo.net/careersection/ex/jobdetail.ftl?job=195223&lang=en") == "hdr:ex"
    assert taleo.board_of("https://fa009.taleo.net/careersection/ex/jobdetail.ftl") == "fa009:ex"
    assert taleo.board_of("https://textron.taleo.net/careersection/textron/moresearch.ftl") == "textron:textron"
    assert (
        taleo.board_of("https://TI.taleo.net/careersection/ti_ex_standard/jobdetail.ftl?job=2200029Z")
        == "ti:ti_ex_standard"
    )
    for bad in (
        "https://hdr.taleo.net.evil.com/careersection/ex/jobsearch.ftl",
        "https://eviltaleo.net/careersection/ex/jobsearch.ftl",
        "https://a.b.taleo.net/careersection/ex/jobsearch.ftl",
        "https://taleo.net/careersection/ex/jobsearch.ftl",
        "https://hdr.taleo.net/careersection/rest/jobboard/searchjobs",
        "https://dasstateoh.taleo.net/careersection/jobdetail.ftl?job=145927",
        "https://hdr.taleo.net/careersection/ex/other.ftl",
        "https://tbe.taleo.net/careersection/ex/jobsearch.ftl",
        "https://www.hdrinc.com/careers",
        "ftp://hdr.taleo.net/careersection/ex/jobsearch.ftl",
        "",
        "nope",
    ):
        assert taleo.board_of(bad) is None, bad


def test_slug_validation():
    assert taleo.parse_slug("HDR:ex") == ("hdr", "ex")
    for bad in ("../x", "a/b", "evil.com#", "hdr", "hdr:ex:x", "hdr:../x", "hdr:a/b", "evil.com#:ex", "hdr:", ":ex"):
        with pytest.raises(ValueError):
            taleo.parse_slug(bad)
    with pytest.raises(ValueError):
        taleo.fetch("../x")


def test_describe_rejects_other_links():
    from appa.feeds import Listing

    with pytest.raises(ValueError):
        taleo.describe(Listing(company="X", title="Y", url="https://evil.com/careersection/ex/jobdetail.ftl?job=1"))
