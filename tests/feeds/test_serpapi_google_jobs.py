"""SerpApi Google Jobs, from a response in the API's documented shape (no key was available to record one)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from appa.feeds import FeedOptions, boards_registry, lists_registry
from appa.feeds.lists import serpapi_google_jobs as feed


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """A fake key, and no real sleeping."""
    monkeypatch.setenv("SERPAPI_API_KEY", "secret-key")
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


def _row(i: int, link: str | None = None) -> dict:
    """A synthetic result with one company apply link."""
    return {
        "title": "SWE",
        "company_name": "Co",
        "detected_extensions": {"posted_at": "1 day ago"},
        "apply_options": [{"title": "Co", "link": link or f"https://careers.co.example/jobs/{1000 + i}"}],
    }


def _serve(monkeypatch, pages: dict) -> list:
    """Serve pages[(q, next_page_token)]; return the list of (q, token) asked for."""
    asked = []

    def fake(url, params=None, headers=None):
        """One canned search page."""
        k = (params["q"], params.get("next_page_token"))
        asked.append(k)
        return pages.get(k, {"jobs_results": []})

    monkeypatch.setattr(feed, "get_json", fake)
    return asked


def test_registered_as_opt_in_list_not_a_board():
    f = lists_registry()["serpapi_google_jobs"]
    assert (f.key_env, f.default) == ("SERPAPI_API_KEY", False)
    assert "serpapi_google_jobs" not in boards_registry()


def test_parses_fixture(feed_http, load_fixture):
    feed_http[feed.API] = load_fixture("serpapi_google_jobs.json")
    out = feed.fetch(FeedOptions(queries=("software engineer new grad",), max_results=5))
    assert [x.company for x in out] == [
        "Figma",
        "Blue Bottle Coffee",
        "GeoLogics Corporation",
    ]  # LinkedIn-only, no-title dropped
    fig, bb, geo = out
    assert (
        fig.url == "https://job-boards.greenhouse.io/figma/jobs/5551234004"
    )  # ATS link beats Indeed/LinkedIn, utm gone
    assert (fig.ats, fig.ats_job_id, fig.source_ref) == ("greenhouse", "5551234004", "Qm9GaWdtYU5ld0dyYWQAAA==")
    assert fig.key == "greenhouse:5551234004"
    assert fig.posted_at == date.today() - timedelta(days=2)
    assert fig.hints["salary"] == "$140K–$180K a year" and fig.hints["employment_type"] == "full_time"
    assert fig.hints["links"] == "https://www.indeed.com/viewjob?jk=abc123def456"
    assert "linkedin" not in fig.model_dump_json().lower()
    assert "[HIDDEN-ON-PAGE]" in fig.description and "new grad" in fig.description
    assert bb.ats == "lever" and bb.ats_job_id == "0b6f7c3e-1a2b-4c5d-9e8f-123456789abc"
    assert bb.posted_at == date.today() - timedelta(days=25)
    assert geo.url.startswith("https://www.dice.com/job-detail/9c29da4b") and geo.ats == "unknown"
    assert geo.hints == {"employment_type": "contract", "remote": "yes"} and geo.posted_at == date.today()
    p = feed_http.calls[0].url.params
    assert (p["engine"], p["q"], p["location"], p["gl"], p["hl"], p["api_key"]) == (
        "google_jobs",
        "software engineer new grad",
        "United States",
        "us",
        "en",
        "secret-key",
    )


def test_no_site_id_leaves_ats_job_id_empty(feed_http):
    feed_http[feed.API] = {
        "jobs_results": [{**_row(1, "https://acme.example/careers/backend-engineer"), "job_id": "googleid123"}]
    }
    (x,) = feed.fetch(FeedOptions(queries=("swe",)))
    assert (x.ats_job_id, x.source_ref, x.key) == (None, "googleid123", "https://acme.example/careers/backend-engineer")


def test_clean_only_strips_utm():
    raw = "https://x.wd5.myworkdayjobs.com/en-US/X/job/A_R1?a=b%2Fc&q=1+2&e=&s=x;y"
    assert feed._clean(raw) == raw
    assert feed._clean(raw + "&utm_campaign=google_jobs_apply&utm_source=g") == raw
    assert feed._clean("https://a.example/j?utm_source=g") == "https://a.example/j"


def test_one_search_per_query_deduped(monkeypatch, load_fixture):
    first = {**json.loads(load_fixture("serpapi_google_jobs.json")), "serpapi_pagination": {"next_page_token": "T2"}}
    asked = _serve(
        monkeypatch,
        {
            ("a", None): first,
            ("a", "T2"): {
                "jobs_results": [_row(i) for i in range(10)],
                "serpapi_pagination": {"next_page_token": "T3"},
            },
            ("b", None): {"jobs_results": [_row(0), _row(20)]},
        },
    )
    out = feed.fetch(FeedOptions(queries=("a", " b ", "a"), max_results=100))
    assert len(out) == 14  # 3 + 10 + one new from "b"
    assert asked == [("a", None), ("a", "T2"), ("b", None)]  # MAX_PAGES per query, queries deduped


def test_pagination_stops_on_empty_page_and_call_cap(monkeypatch):
    asked = _serve(
        monkeypatch,
        {
            ("a", None): {"jobs_results": [_row(1)], "serpapi_pagination": {"next_page_token": "T2"}},
            ("a", "T2"): {"jobs_results": [], "serpapi_pagination": {"next_page_token": "T3"}},
        },
    )
    monkeypatch.setattr(feed, "MAX_PAGES", 10)
    assert len(feed.fetch(FeedOptions(queries=("a",)))) == 1
    assert asked == [("a", None), ("a", "T2")]
    asked = _serve(monkeypatch, {(q, None): {"jobs_results": [_row(i)]} for i, q in enumerate("abcdefgh")})
    assert len(feed.fetch(FeedOptions(queries=tuple("abcdefgh")))) == feed.MAX_CALLS
    assert len(asked) == feed.MAX_CALLS


def test_max_results_and_default_queries(monkeypatch):
    asked = _serve(
        monkeypatch,
        {
            (q, None): {"jobs_results": [_row(i) for i in range(10)], "serpapi_pagination": {"next_page_token": "T"}}
            for q in feed.QUERIES
        },
    )
    assert len(feed.fetch(FeedOptions(queries=(), max_results=5))) == 5
    assert asked == [(feed.QUERIES[0], None)]


def test_empty_results_and_since(feed_http):
    feed_http[feed.API] = {"error": "Google hasn't returned any results for this query."}
    assert feed.fetch(FeedOptions(queries=("zzzz",))) == []
    feed_http[feed.API] = {"jobs_results": [_row(1), {**_row(2), "detected_extensions": {"posted_at": "2 weeks ago"}}]}
    out = feed.fetch(FeedOptions(queries=("swe",), since=date.today() - timedelta(days=5), us_only=False))
    assert len(out) == 1
    p = feed_http.calls[-1].url.params
    assert p["q"] == "swe in the last week" and "location" not in p and "gl" not in p


def test_malformed_rows_skipped(feed_http):
    feed_http[feed.API] = {
        "jobs_results": [
            "junk",
            {"title": "x"},
            {**_row(1), "company_name": ""},
            {
                **_row(2),
                "apply_options": [{"link": "javascript:alert(1)"}, {"link": "https://in.linkedin.com/jobs/view/1"}],
            },
            _row(3),
        ]
    }
    assert [x.url for x in feed.fetch(FeedOptions(queries=("swe",)))] == ["https://careers.co.example/jobs/1003"]


def test_errors_hide_the_key(feed_http, monkeypatch):
    opts = FeedOptions(queries=("swe",))
    feed_http[feed.API] = httpx.Response(401, content=b'{"error":"Invalid API key."}')
    with pytest.raises(RuntimeError) as e:
        feed.fetch(opts)
    assert "secret-key" not in str(e.value) and "401" in str(e.value)
    feed_http[feed.API] = {"error": "Your account has run out of searches."}
    with pytest.raises(RuntimeError, match="run out"):
        feed.fetch(opts)
    monkeypatch.delenv("SERPAPI_API_KEY")
    with pytest.raises(RuntimeError, match="SERPAPI_API_KEY is not set"):
        feed.fetch(opts)
