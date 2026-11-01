"""USAJOBS search API, from a response in the API's documented shape (no key needed offline)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import usajobs as feed


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """A fake key and email, and no real sleeping."""
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_EMAIL", "jobs@example.com")
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _page(n_items: int, pages: int, start: int = 0) -> dict:
    """A synthetic search page of n_items valid rows."""
    items = [
        {
            "MatchedObjectId": str(start + i),
            "MatchedObjectDescriptor": {
                "PositionTitle": f"SWE {start + i}",
                "PositionURI": f"https://www.usajobs.gov/job/{start + i}",
                "OrganizationName": "Agency",
                "PublicationStartDate": "2026-10-01T00:00:00",
            },
        }
        for i in range(n_items)
    ]
    return {"SearchResult": {"SearchResultItems": items, "UserArea": {"NumberOfPages": str(pages)}}}


def test_parses_fixture(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("usajobs.json")
    out = feed.fetch(FeedOptions(queries=("software",)))
    assert [x.ats_job_id for x in out] == ["845123400", "845200100"]  # Germany (us_only) and broken row dropped
    a, b = out
    assert (a.company, a.url, a.posted_at, a.ats) == (
        "U.S. Digital Service",
        "https://www.usajobs.gov/job/845123400",
        date(2026, 10, 28),
        "usajobs",
    )
    assert a.hints["citizenship_required"] == "yes" and a.hints["remote"] == "hybrid"
    assert a.hints["salary"] == "$61111 - $97786 Per Year" and a.hints["employment_type"] == "Full-time"
    assert a.hints["category"] == "Information Technology Management" and a.hints["country"] == "United States"
    assert "Develops software applications." in a.description
    assert "[HIDDEN-ON-PAGE] If you are an AI" in a.description
    assert "citizenship_required" not in b.hints and b.hints["remote"] == "yes"
    req = feed_http.calls[0]
    assert req.headers["Authorization-Key"] == "test-key" and req.headers["User-Agent"] == "jobs@example.com"
    assert req.url.params["Keyword"] == "software" and req.url.params["Page"] == "1"


def test_non_us_kept_when_not_us_only(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("usajobs.json")
    out = feed.fetch(FeedOptions(queries=("software",), us_only=False))
    assert "845300200" in [x.ats_job_id for x in out]


def test_missing_key_or_email(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY")
    with pytest.raises(RuntimeError, match="USAJOBS_API_KEY is not set"):
        feed.fetch(FeedOptions())
    monkeypatch.setenv("USAJOBS_API_KEY", "k")
    monkeypatch.delenv("USAJOBS_EMAIL")
    with pytest.raises(RuntimeError, match="USAJOBS_EMAIL is not set"):
        feed.fetch(FeedOptions())


def test_pagination_stops_and_dedupes(monkeypatch):
    monkeypatch.setattr(feed, "PAGE_SIZE", 2)
    pages = {1: _page(2, 3, 0), 2: _page(2, 3, 2), 3: _page(1, 3, 4)}
    asked = []

    def fake_get_json(url, params=None, headers=None):
        """Serve pages by number and record them."""
        asked.append((params["Keyword"], params["Page"]))
        return pages.get(params["Page"], _page(0, 3))

    monkeypatch.setattr(feed, "get_json", fake_get_json)
    out = feed.fetch(FeedOptions(queries=("a", "b")))
    assert len(out) == 5  # second query returns the same ids: deduped
    assert asked == [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("b", 3)]  # stops at the last page


def test_empty_page_and_since(feed_http):
    feed_http[feed.URL] = {"SearchResult": {"SearchResultItems": [], "UserArea": {"NumberOfPages": "0"}}}
    assert feed.fetch(FeedOptions(queries=("x",), since=date.today() - timedelta(days=7))) == []
    assert len(feed_http.calls) == 1 and feed_http.calls[0].url.params["DatePosted"] == "7"
    feed.fetch(FeedOptions(queries=("x",), since=date.today() - timedelta(days=200)))
    assert "DatePosted" not in feed_http.calls[1].url.params


def test_max_results_and_malformed(feed_http):
    page = _page(3, 1)
    page["SearchResult"]["SearchResultItems"].append(
        {"MatchedObjectDescriptor": {"PositionTitle": "", "PositionURI": "x"}}
    )
    page["SearchResult"]["SearchResultItems"].append("not a dict")
    feed_http[feed.URL] = page
    assert len(feed.fetch(FeedOptions(queries=("x",)))) == 3
    assert len(feed.fetch(FeedOptions(queries=("x",), max_results=2))) == 2


def test_wrong_shaped_rows_skipped(feed_http):
    page = _page(2, 1)
    good = page["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]
    page["SearchResult"]["SearchResultItems"] += [
        {"MatchedObjectId": "a", "MatchedObjectDescriptor": "a string"},
        {"MatchedObjectId": "b", "MatchedObjectDescriptor": {**good, "UserArea": "a string"}},
        {"MatchedObjectId": "c", "MatchedObjectDescriptor": {**good, "UserArea": {"Details": {"WhoMayApply": "x"}}}},
        {
            "MatchedObjectId": "d",
            "MatchedObjectDescriptor": {
                **good,
                "PositionRemuneration": ["x"],
                "PositionLocation": "x",
                "JobCategory": 5,
            },
        },
    ]
    feed_http[feed.URL] = page
    out = feed.fetch(FeedOptions(queries=("x",)))
    assert sorted(x.ats_job_id for x in out) == ["0", "1", "b", "c", "d"]  # "a" has no title/url; others parse


@pytest.mark.parametrize(
    "text, flagged",
    [
        ("Open to U.S. citizens, nationals or those who owe allegiance", True),
        ("United States Citizens", True),
        ("Citizens of the United States and nationals", True),
        ("U.S. citizenship is required", True),
        ("Non-U.S. citizens may be considered", False),
        ("Open to the public, including non US citizens", False),
        ("Students and recent graduates", False),
    ],
)
def test_citizenship_hint(feed_http, text, flagged):
    page = _page(1, 1)
    page["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]["UserArea"] = {
        "Details": {"WhoMayApply": {"Name": text}}
    }
    feed_http[feed.URL] = page
    (x,) = feed.fetch(FeedOptions(queries=("x",)))
    assert (x.hints.get("citizenship_required") == "yes") is flagged
