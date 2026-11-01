"""NoDesk RSS boards, from a recorded slice of the real main feed."""

from __future__ import annotations

from datetime import date

import pytest
from defusedxml import EntitiesForbidden

from appa.feeds import FeedOptions
from appa.feeds.boards import nodesk_rss as feed

MAIN = "https://nodesk.co/remote-jobs/index.xml"
ENG = "https://nodesk.co/remote-jobs/engineering/index.xml"

ITEM = (
    "<item><title>{title}</title><description>x</description><pubDate>Thu, 29 Oct 2026 08:00:00 +0200</pubDate>"
    "<link>{link}</link></item>"
)


def _feed(*items: str) -> str:
    """A minimal NoDesk-shaped RSS document."""
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>NoDesk</title>{"".join(items)}</channel></rss>'


def test_reads_items(feed_http, load_fixture):
    feed_http[MAIN] = load_fixture("nodesk_rss.xml")
    out = feed.fetch("all")
    assert len(out) == 4 and [str(c.url) for c in feed_http.calls] == [MAIN]
    rc = out[0]
    assert (rc.company, rc.title) == ("Rocket.chat", "Marketing Manager - Defense & Intelligence (NORAM)")
    assert rc.url == "https://nodesk.co/remote-jobs/rocket-chat-marketing-manager-defense-intelligence-noram/"
    assert rc.ats_job_id == rc.source_ref == "rocket-chat-marketing-manager-defense-intelligence-noram"
    assert rc.posted_at == date(2026, 10, 30) and rc.location == "Remote (United States)"
    assert rc.hints == {"remote": "yes"} and rc.ats == "unknown"
    ramp = out[2]
    assert (ramp.company, ramp.title) == ("Ramp", "Software Engineer, Production Engineering")
    assert ramp.posted_at == date(2026, 10, 29) and ramp.location == "Remote"
    assert "Ramp is building" in ramp.description
    assert "we’ve been certified" in out[3].description  # &rsquo; survived parsing
    assert all("index.xml" not in x.url for x in out)


def test_category_feed_hint(feed_http, load_fixture):
    feed_http[ENG] = load_fixture("nodesk_rss.xml")
    out = feed.fetch("engineering")
    assert str(feed_http.calls[0].url) == ENG and out[0].hints["category"] == "Engineering"


def test_since_cap_dupes_and_malformed(feed_http):
    good = ITEM.format(title="SWE at Acme", link="https://nodesk.co/remote-jobs/acme-swe/")
    old = ITEM.replace("29 Oct", "01 Sep").format(title="Old at Acme", link="https://nodesk.co/remote-jobs/acme-old/")
    bad = [
        ITEM.format(title="No company here", link="https://nodesk.co/remote-jobs/x-y/"),
        ITEM.format(title="SWE at Evil", link="https://evil.com/remote-jobs/evil-swe/"),
        ITEM.format(title="SWE at Evil", link="javascript:alert(1)"),
        ITEM.format(title="Cat at X", link="https://nodesk.co/remote-jobs/engineering/"),
        ITEM.format(title=" at Acme", link="https://nodesk.co/remote-jobs/acme-blank/"),
        ITEM.format(title="SWE at Acme", link="https://nodesk.co/remote-jobs/a/b/"),
    ]
    feed_http[MAIN] = _feed(good, *bad, good, old)
    assert [x.ats_job_id for x in feed.fetch("all")] == ["acme-swe", "acme-old"]
    assert [x.ats_job_id for x in feed.fetch("all", FeedOptions(since=date(2026, 10, 1)))] == ["acme-swe"]
    assert len(feed.fetch("all", FeedOptions(max_results=1))) == 1
    feed_http[MAIN] = _feed()
    assert feed.fetch("all") == []


def test_one_request_no_paging(feed_http, load_fixture):
    feed_http[MAIN] = load_fixture("nodesk_rss.xml")
    feed.fetch("all")
    assert len(feed_http.calls) == 1


def test_unknown_entities_and_errors(feed_http):
    import httpx

    feed_http[MAIN] = _feed(
        ITEM.format(title="Dev &bogus; &hellip; at Acme", link="https://nodesk.co/remote-jobs/acme-dev/")
    )
    assert feed.fetch("all")[0].title == "Dev &bogus; …"
    feed_http[MAIN] = '<!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>'
    with pytest.raises(EntitiesForbidden):
        feed.fetch("all")  # defusedxml refuses DTD entities
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch("finance")  # unrouted -> 404


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://nodesk.co/remote-jobs/", "all"),
        ("https://www.nodesk.co/remote-jobs", "all"),
        ("https://nodesk.co/remote-jobs/index.xml", "all"),
        ("https://nodesk.co/remote-jobs/engineering/", "engineering"),
        ("http://nodesk.co/remote-jobs/customer-support/index.xml", "customer-support"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://nodesk.co/remote-jobs/ramp-software-engineer-production-engineering/",  # a job page, not a board
        "https://nodesk.co/remote-jobs/us/",
        "https://nodesk.co/remote-companies/ramp/",
        "https://nodesk.co/",
        "https://evilnodesk.co/remote-jobs/",
        "https://nodesk.co.evil.com/remote-jobs/",
        "https://x.nodesk.co/remote-jobs/",
        "ftp://nodesk.co/remote-jobs/",
        "https://boards.greenhouse.io/figma",
        "not a url",
        "",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-a", "a" * 51, "a b", "Engineering", "x?y"])
def test_slug_validation(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
