"""Workopia US new-grad list, from a recorded slice of the real file."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import workopia_us_newgrad as feed


def test_keeps_tech_data_grad_non_intern_rows(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("workopia_us_newgrad.json")
    out = feed.fetch(FeedOptions())
    assert [x.company for x in out] == ["Lockheed Martin", "Western Power", "Ao Ds"]
    swe = out[0]
    assert swe.title == "Software Engineer Asc - Early Career"
    assert (
        swe.url
        == "https://workopia.io/jobs/fd7204fedb1334f93f8be30cd9a95da3"
        == "https://workopia.io/jobs/" + swe.ats_job_id
    )
    assert swe.ats_job_id == swe.source_ref and swe.posted_at == date(2026, 10, 29)
    assert swe.hints == {"category": "Tech", "level": "new_grad", "country": "US", "salary": "$67,000–124,400"}
    assert "salary" not in out[2].hints and out[2].hints["level"] == "entry"  # "*" = Workopia estimate
    assert out[1].hints["category"] == "Data, AI & ML"
    assert all(x.title != "Electrical Apprentice" for x in out)  # non-tech grad program dropped
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    assert all(x.description == "" for x in out)


def _row(**kw) -> dict:
    """A minimal in-scope row, overridden by kw."""
    return {
        "id": "abc123",
        "company_name": "A",
        "title": "SWE",
        "url": "https://workopia.io/jobs/abc123",
        "category": "tech",
        "tier": "Graduate",
        "active": True,
        "date_posted": "2026-10-01T00:00:00.000Z",
        "location": "Remote",
        "sponsorship": True,
        **kw,
    }


def test_since_dedupe_and_malformed_rows(feed_http):
    feed_http[feed.URL] = {
        "meta": {},
        "listings": [
            _row(),
            _row(company_name="Other", date_posted="2026-09-30T00:00:00Z"),  # same id: collapses to one
            _row(id="dup1", company_name="A", title="SWE", date_posted="2026-09-29T00:00:00Z"),  # same title, new id
            _row(id="old", company_name="B", date_posted="2025-01-01T00:00:00Z"),
            _row(id="x", company_name="C", url="javascript:alert(1)"),  # upstream url is ignored
            _row(id="y", company_name=""),
            _row(id="z", company_name="D", active=False),
            _row(id="w", company_name="E", tier="Intern"),
            _row(id="v", company_name="F", category="sales"),
            _row(id="../bad", company_name="G"),
            _row(id="", company_name="G2"),
            _row(id="g1", company_name="H", category="grad", title="Manager Trainee"),
            _row(id="g2", company_name="I", category="grad", title="Software Engineer Rotation Program"),
            _row(id="n", company_name="J", date_posted="not a date"),
            "not a row",
        ],
    }
    out = feed.fetch(FeedOptions(since=date(2026, 1, 1)))
    assert [(x.company, x.ats_job_id) for x in out] == [
        ("A", "abc123"),
        ("C", "x"),
        ("I", "g2"),
        ("A", "dup1"),
        ("J", "n"),
    ]
    assert out[0].hints["remote"] == "yes" and out[0].hints["sponsorship"] == "Offers Sponsorship"
    assert out[1].url == "https://workopia.io/jobs/x"
    assert out[2].hints["category"] == "New Grad Programs" and out[4].posted_at is None
    assert len(feed.fetch(FeedOptions(max_results=1))) == 1


def test_bad_shape_and_http_errors_raise(feed_http):
    feed_http[feed.URL] = [{"not": "the expected object"}]
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())
    feed_http[feed.URL] = httpx.Response(500)
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch(FeedOptions())


def test_fixture_is_small_and_real(load_fixture):
    data = json.loads(load_fixture("workopia_us_newgrad.json"))
    assert 3 <= len(data["listings"]) <= 6 and len(load_fixture("workopia_us_newgrad.json")) < 60_000
