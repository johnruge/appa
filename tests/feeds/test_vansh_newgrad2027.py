"""vanshb03/New-Grad-2027 list, from a recorded slice of the real file."""

from __future__ import annotations

import json
import time
from datetime import date

from appa.feeds import FeedOptions, lists_registry
from appa.feeds.lists import vansh_newgrad2027 as feed


def test_reads_active_listings_with_hints(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("vansh_newgrad2027.json")
    out = feed.fetch(FeedOptions(since=date(2025, 1, 1)))
    rows = json.loads(load_fixture("vansh_newgrad2027.json"))
    assert len(out) == len(rows) - 1  # the closed Red Hat row is dropped
    assert "Red Hat" not in {x.company for x in out}
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    first = out[0]
    assert (first.company, first.url, first.posted_at) == ("Ciena", "https://go.vansh.nyc/10O", date(2026, 10, 29))
    assert first.source_ref == "ba3da99f-2320-4ea3-9b19-e7f69a47fb85" and first.ats_job_id is None
    assert first.hints["country"] == "Canada" and "sponsorship" not in first.hints  # "Other" isn't a label
    assert all(x.hints["level"] == "new_grad" and x.url.startswith("https://go.vansh.nyc/") for x in out)
    by = {x.company: x for x in out}
    assert by["Palantir"].hints["citizenship_required"] == "yes"
    assert by["Lumafield"].hints["sponsorship"] == "Does Not Offer Sponsorship"
    assert "citizenship_required" not in by["Lumafield"].hints
    assert by["Softheon"].hints["remote"] == "yes" and by["Softheon"].location == "Remote; United States"
    assert len(feed_http.calls) == 1  # shortlinks are not resolved


def test_no_default_cutoff_and_since_filters(feed_http, load_fixture):
    rows = json.loads(load_fixture("vansh_newgrad2027.json"))
    rows.append({**rows[0], "id": "fresh", "company_name": "Fresh", "date_posted": int(time.time()) - 86400})
    feed_http[feed.URL] = rows
    names = [x.company for x in feed.fetch(FeedOptions())]
    assert {"Fresh", "Tesla", "Palantir"} <= set(names)  # old but still active rows are kept
    assert len(names) == len(rows) - 1
    recent = [x.company for x in feed.fetch(FeedOptions(since=date(2026, 1, 1)))]
    assert "Fresh" in recent and "Tesla" not in recent and "Palantir" not in recent
    assert len(feed.fetch(FeedOptions(max_results=2))) == 2


def test_malformed_rows_skipped(feed_http):
    feed_http[feed.URL] = [
        {
            "active": True,
            "is_visible": True,
            "company_name": "A",
            "title": "SWE",
            "url": "https://go.vansh.nyc/a",
            "date_posted": 1767225600,
            "locations": ["NYC"],
        },  # 2026-01-01
        {
            "active": True,
            "is_visible": True,
            "company_name": "B",
            "title": "SWE",
            "url": "https://go.vansh.nyc/b",
            "date_posted": "garbage",
            "locations": None,
        },
        {"active": True, "company_name": "C", "title": "SWE", "url": "javascript:alert(1)"},
        {"active": True, "company_name": "", "title": "SWE", "url": "https://go.vansh.nyc/d"},
        {"active": True, "is_visible": False, "company_name": "E", "title": "SWE", "url": "https://go.vansh.nyc/e"},
        {"active": True, "company_name": "F", "title": "x" * 600, "url": "https://go.vansh.nyc/f"},
        "not a row",
    ]
    out = feed.fetch(FeedOptions(since=date(2025, 6, 1)))
    assert [x.company for x in out] == ["A", "B"] and out[1].posted_at is None


def test_registered():
    assert lists_registry()["vansh_newgrad2027"].fetch is feed.fetch
