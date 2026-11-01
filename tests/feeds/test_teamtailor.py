"""Teamtailor career sites, from a recorded slice of a real jobs RSS feed."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import teamtailor as feed

URL = "https://career.teamtailor.com/jobs.rss"
FIX = "teamtailor.rss"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No sleeping between pages."""
    monkeypatch.setattr(feed, "PAGE_SLEEP", 0)


def _item(
    jid: int,
    title: str = "Software Engineer",
    remote: str = "none",
    pub: str = "Thu, 29 Oct 2026 10:00:00 +0100",
    link: str | None = None,
) -> str:
    """One RSS item in Teamtailor's shape."""
    link = link if link is not None else f"https://jobs.acme.example/jobs/{jid}-software-engineer"
    return (
        f"<item><title>{title}</title><description>&lt;p&gt;Build things&lt;/p&gt;</description>"
        f"<pubDate>{pub}</pubDate><link>{link}</link><remoteStatus>{remote}</remoteStatus><guid>g-{jid}</guid>"
        f"<tt:locations><tt:location><tt:name>NYC</tt:name><tt:city>New York</tt:city>"
        f"<tt:country>United States</tt:country></tt:location></tt:locations>"
        f"<tt:department>Engineering</tt:department></item>"
    )


def _rss(*items: str, title: str = "Acme") -> str:
    """An RSS document around the given items."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations">'
        f"<channel><title>{title}</title>{''.join(items)}</channel></rss>"
    )


def test_parses_fixture(feed_http, load_fixture):
    feed_http[URL] = load_fixture(FIX)
    out = feed.fetch("career")
    assert len(out) == 4 and len(feed_http.calls) == 1  # fewer than a page: no second request
    q = feed_http.calls[0].url.params
    assert (q["per_page"], q["offset"]) == ("100", "0")
    sa = out[0]
    assert (sa.company, sa.title) == ("Teamtailor", "Solution Architect (Bilingual French/English)")
    assert sa.url == "https://career.teamtailor.com/jobs/8636263-solution-architect-bilingual-french-english"
    assert (
        sa.ats == "teamtailor"
        and sa.ats_job_id == "8636263"
        and sa.source_ref == "5c189e36-797b-4e58-8540-defb85b3668a"
    )
    assert sa.posted_at == date(2026, 10, 29) and sa.location == "Stockholm, Sweden; Paris, France"
    assert sa.hints == {"remote": "hybrid", "country": "Sweden, France", "category": "Product Specialists"}
    assert "Solution Architect" in sa.description and "<p" not in sa.description and "&lt;" not in sa.description
    assert out[1].location == "Chicago, United States" and out[1].hints["country"] == "United States"
    assert out[3].hints["remote"] == "no" and out[3].posted_at == date(2026, 10, 14)
    assert all(x.url.startswith("https://career.teamtailor.com/jobs/") and x.ats_job_id for x in out)


def test_pagination_stops(feed_http):
    page1 = _rss(*[_item(i) for i in range(100)])
    page2 = _rss(*[_item(i) for i in range(100, 130)])
    feed_http[URL + "?per_page=100&offset=0"] = page1
    feed_http[URL + "?per_page=100&offset=100"] = page2
    out = feed.fetch("career")
    assert len(out) == 130 and len(feed_http.calls) == 2
    assert out[0].company == "Acme" and out[0].hints["category"] == "Engineering"
    feed_http[URL + "?per_page=100&offset=100"] = page1  # a site ignoring offset: no new IDs, stop
    assert len(feed.fetch("career")) == 100
    assert len(feed.fetch("career", FeedOptions(max_results=5))) == 5


def test_since_remote_and_malformed_rows(feed_http):
    feed_http[URL] = _rss(
        _item(1, remote="fully"),
        _item(2, pub="Mon, 05 Jan 2026 10:00:00 +0000"),
        _item(3, link="javascript:alert(1)"),
        _item(4, title=""),
        _item(1),  # duplicate ID
        _item(5, pub="not a date"),
    )
    out = feed.fetch("career")
    assert [x.ats_job_id for x in out] == ["1", "2", "5"]
    assert out[0].hints["remote"] == "yes" and out[0].location == "Remote (New York, United States)"
    assert out[2].posted_at is None
    assert [x.ats_job_id for x in feed.fetch("career", FeedOptions(since=date(2026, 10, 1)))] == ["1", "5"]
    feed_http[URL] = _rss()
    assert feed.fetch("career") == []


def test_http_errors_raise(feed_http):
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("nosuchboard")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://career.teamtailor.com/jobs/8636263-solution-architect", "career"),
        ("https://Polestar.teamtailor.com/", "polestar"),
        ("http://veidekke.teamtailor.com/jobs.rss", "veidekke"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilteamtailor.com/jobs/1",
        "https://tibber.teamtailor.com.evil.com/jobs/1",
        "https://teamtailor.com/",
        "https://www.teamtailor.com/en-us/",
        "https://app.teamtailor.com/",
        "https://support.teamtailor.com/en/articles/1",
        "https://a.b.teamtailor.com/",
        "https://jobs.tibber.com/jobs/8632993-talent-acquisition-partner",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a-", "a" * 64, "a b", "x.teamtailor.com"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
