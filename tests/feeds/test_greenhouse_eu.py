"""Greenhouse EU boards: link recognition and fetching through the global API, from a recorded bitpanda board."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from appa.feeds import FeedOptions, boards_registry
from appa.feeds import board_of as any_board_of
from appa.feeds.boards import greenhouse_eu as feed

API = "https://boards-api.greenhouse.io/v1/boards/"


def test_reads_eu_board_through_global_api(feed_http, load_fixture):
    feed_http[API + "bitpanda/jobs"] = load_fixture("greenhouse_eu.json")
    out = feed.fetch("bitpanda")
    assert len(out) == 4
    first = out[0]
    assert first.company == "Bitpanda" and first.title == "Associate, AFC Compliance"
    assert first.url == "https://job-boards.eu.greenhouse.io/bitpanda/jobs/5012230101"
    assert first.ats == "greenhouse" and first.ats_job_id == "5012230101"
    assert first.location == "Vienna, Vienna, Austria"
    assert first.description and "<" not in first.description[:200]
    assert first.key == "greenhouse:5012230101"
    assert first.posted_at == date(2026, 10, 22)
    assert first.source_ref == "5012230101"
    assert [x.posted_at for x in out] == [date(2026, 10, 22), date(2026, 9, 7), date(2025, 12, 2), date(2026, 10, 14)]
    assert all(x.url.startswith("https://job-boards.eu.greenhouse.io/bitpanda/jobs/") for x in out)
    full, light = feed_http.calls  # postings with content, then the light date listing
    assert full.url.host == "boards-api.greenhouse.io" and full.url.params["content"] == "true"
    assert light.url.host == "boards-api.greenhouse.io" and "content" not in light.url.params


def test_empty_board_and_bad_rows(feed_http):
    feed_http[API + "empty/jobs"] = {"jobs": [], "meta": {"total": 0}}
    assert feed.fetch("empty") == []
    feed_http[API + "mixed/jobs"] = {
        "jobs": [
            {
                "id": 1,
                "title": "SWE",
                "company_name": "A",
                "absolute_url": "https://job-boards.eu.greenhouse.io/mixed/jobs/1",
            },
            {"id": 2, "title": "SWE", "company_name": "A", "absolute_url": "javascript:alert(1)"},
        ]
    }
    assert [x.ats_job_id for x in feed.fetch("mixed")] == ["1"]
    assert len(feed_http.calls) == 3  # empty board: no date request


def test_dates_are_best_effort(feed_http, load_fixture):
    full = load_fixture("greenhouse_eu.json")
    feed_http[API + "bitpanda/jobs?content=true"] = full
    feed_http[API + "bitpanda/jobs"] = httpx.Response(500)  # date listing fails: still a board
    out = feed.fetch("bitpanda")
    assert len(out) == 4 and all(x.posted_at is None for x in out)
    feed_http[API + "bitpanda/jobs"] = {
        "jobs": [
            {"id": 5012230101, "first_published": "garbage", "updated_at": "2026-10-27T09:48:56-04:00"},
            {"id": 4969789101, "first_published": "nope"},
            "not a dict",
            {"first_published": "2026-01-01T00:00:00Z"},
        ]
    }
    out = feed.fetch("bitpanda")
    assert [x.posted_at for x in out] == [date(2026, 10, 27), None, None, None]


def test_since_and_max_results(feed_http, load_fixture):
    feed_http[API + "bitpanda/jobs"] = load_fixture("greenhouse_eu.json")
    newer = feed.fetch("bitpanda", FeedOptions(since=date(2026, 10, 1)))
    assert [x.ats_job_id for x in newer] == ["5012230101", "5005854101"]
    assert len(feed.fetch("bitpanda", FeedOptions(max_results=2))) == 2
    assert len(feed_http.calls) == 4  # two requests per board, no pagination


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://job-boards.eu.greenhouse.io/bitpanda", "bitpanda"),
        ("https://job-boards.eu.greenhouse.io/imc/jobs/4979079101", "imc"),
        ("https://job-boards.eu.greenhouse.io/theeconomistgroup/jobs/4996339101?gh_src=x", "theeconomistgroup"),
        ("https://job-boards.eu.greenhouse.io/embed/job_board?for=groww", "groww"),
        ("https://job-boards.eu.greenhouse.io/embed/job_app?for=plos&token=4974895101", "plos"),
        ("https://boards.eu.greenhouse.io/cision", "cision"),
        ("https://JOB-BOARDS.EU.GREENHOUSE.IO/bitpanda/", "bitpanda"),
        ("https://job-boards.greenhouse.io/figma/jobs/123", "figma"),
        ("https://boards.greenhouse.io/embed/job_board?for=figma", "figma"),
    ],
)
def test_board_of_positives(url, slug):
    assert feed.board_of(url) == slug
    assert any_board_of(url) == ("greenhouse", slug)  # the registered greenhouse board uses it


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.eu.greenhouse.io/",
        "https://job-boards.eu.greenhouse.io/embed/job_board",  # no for=
        "https://job-boards.eu.greenhouse.io/embed/job_board?for=../x",
        "https://job-boards.eu.greenhouse.io/embed/other?for=bitpanda",
        "https://job-boards.eu.greenhouse.io.evil.com/bitpanda",
        "https://eviljob-boards.eu.greenhouse.io/bitpanda",
        "https://evilgreenhouse.io/bitpanda",
        "https://example.com/?next=https://job-boards.eu.greenhouse.io/bitpanda",
        "https://www.greenhouse.io/careers",
        "https://bitpanda.com/careers?gh_jid=5012230101",
        "ftp://job-boards.eu.greenhouse.io/bitpanda",
        "not a url",
        "https://job-boards.greenhouse.io/static/x",
        "https://job-boards.greenhouse.io/assets/app.js",
        "https://job-boards.greenhouse.io/figma/other/page",
        "https://evil.com/job-boards.greenhouse.io/figma",
    ],
)
def test_board_of_negatives(url):
    assert feed.board_of(url) is None


def test_registered_as_greenhouse():
    board = boards_registry()["greenhouse"]
    assert board.board_of is feed.board_of and board.fetch is feed.fetch
    assert "greenhouse_eu" not in boards_registry()


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "embed", "static", "a b", "-x", "x" * 101])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
