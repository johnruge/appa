"""ApplyGuy new-grad list, from a recorded slice of the real feed."""

from __future__ import annotations

import json
from datetime import date

from appa.feeds import FeedOptions, lists_registry
from appa.feeds.lists import applyguy_newgrad as feed


def _load(feed_http, load_fixture):
    """Serve the fixture and fetch it."""
    feed_http[feed.URL] = load_fixture("applyguy_newgrad.json")
    return feed.fetch(FeedOptions())


def test_reads_listings_with_direct_links_and_ids(feed_http, load_fixture):
    out = _load(feed_http, load_fixture)
    rows = json.loads(load_fixture("applyguy_newgrad.json"))["jobs"]
    assert len(out) == len(rows) == 6
    assert len(feed_http.calls) == 1
    assert not any("applyguy.ai" in x.url for x in out)
    by = {x.company: x for x in out}
    gh = by["Built Technologies"]
    assert gh.url == "https://job-boards.greenhouse.io/getbuilt/jobs/4749545005"
    assert (gh.ats, gh.ats_job_id) == ("greenhouse", "4749545005")
    assert gh.posted_at == date(2026, 10, 30) and gh.location == "Nashville, TN"
    assert gh.source_ref == "d2ae74e3-21ac-4447-8fce-e94faf6dd843"
    assert gh.hints == {"level": "entry", "category": "Software"}
    wd = by["Pae"]
    assert (wd.ats, wd.ats_job_id) == ("workday", "r0173771") and wd.hints["level"] == "new_grad"
    assert {x.ats for x in out} >= {"greenhouse", "workday", "ashby", "lever", "unknown"}
    assert all(x.ats_job_id for x in out)  # every recorded link carries an ID
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    assert all(x.description == "" for x in out)


def test_keys_dedupe_on_the_ats_id(feed_http, load_fixture):
    out = _load(feed_http, load_fixture)
    keys = {x.key for x in out}
    assert "greenhouse:4749545005" in keys and len(keys) == len(out)


def test_since_cap_and_malformed_rows(feed_http):
    good = {
        "id": "u1",
        "company": "A",
        "title": "SWE",
        "location": "Remote",
        "eligibility": "New Grad",
        "posted": "2026-10-01",
        "url": "https://applyguy.ai/jobs?job=u1",
        "listingUrl": "https://jobs.lever.co/a/eb6ab6cf-6f93-486f-b4bf-6137bc67feb7",
    }
    feed_http[feed.URL] = {
        "updatedAt": "2026-10-30T00:00:00Z",
        "jobs": [
            good,
            {**good, "id": "u2", "company": "B", "posted": "2025-01-01", "listingUrl": "https://b.example/careers/9"},
            {**good, "id": "u3", "company": "C", "listingUrl": ""},  # tracking link only: skipped
            {**good, "id": "u4", "company": "D", "listingUrl": "javascript:alert(1)"},
            {**good, "id": "u5", "company": "", "listingUrl": "https://e.example/1"},
            {**good, "id": "u6", "company": "F", "posted": "soon", "listingUrl": "https://f.example/x"},
            "not a row",
        ],
    }
    out = feed.fetch(FeedOptions())
    assert [x.company for x in out] == ["A", "B", "F"]
    assert out[0].hints["remote"] == "yes" and out[0].ats == "lever"
    assert out[2].posted_at is None and out[2].ats_job_id is None
    assert [x.company for x in feed.fetch(FeedOptions(since=date(2026, 6, 1)))] == ["A", "F"]
    assert len(feed.fetch(FeedOptions(max_results=1))) == 1


def test_bad_shape_raises(feed_http):
    feed_http[feed.URL] = [{"id": "x"}]
    try:
        feed.fetch(FeedOptions())
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_registered():
    assert "applyguy_newgrad" in lists_registry()
