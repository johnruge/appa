"""SimplifyJobs new-grad list, from a recorded slice of the real file."""

from __future__ import annotations

import json
from datetime import date

from appa.feeds import FeedOptions
from appa.feeds.lists import simplify_newgrad as feed


def test_reads_active_listings_with_hints(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("simplify_newgrad.json")
    out = feed.fetch(FeedOptions())
    rows = json.loads(load_fixture("simplify_newgrad.json"))
    assert len(out) == len(rows) - 1  # the closed row is dropped
    assert all(x.hints["level"] == "new_grad" and x.url.startswith("https://") for x in out)
    cit = [x for x in out if x.hints.get("citizenship_required") == "yes"]
    assert len(cit) == 1 and "Citizenship" in cit[0].hints["sponsorship"]
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)


def test_since_and_malformed_rows(feed_http):
    feed_http[feed.URL] = [
        {
            "active": True,
            "is_visible": True,
            "company_name": "A",
            "title": "SWE",
            "url": "https://a.example/1",
            "date_posted": 1767225600,
            "locations": ["NYC"],
        },  # 2026-01-01
        {
            "active": True,
            "is_visible": True,
            "company_name": "B",
            "title": "SWE",
            "url": "https://b.example/1",
            "date_posted": 1735689600,
        },  # 2025-01-01
        {"active": True, "company_name": "C", "title": "SWE", "url": "javascript:alert(1)"},
        {"active": True, "company_name": "", "title": "SWE", "url": "https://d.example/1"},
        {"active": True, "is_visible": False, "company_name": "E", "title": "SWE", "url": "https://e.example/1"},
    ]
    out = feed.fetch(FeedOptions(since=date(2025, 6, 1)))
    assert [x.company for x in out] == ["A"]
