"""Hacker News job stories (Firebase API), from recorded real items; ATS probes are canned."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa import feeds
from appa.feeds import FeedOptions, Listing
from appa.feeds.lists import hn_jobstories as feed

GH = "https://boards-api.greenhouse.io/v1/boards/"
LEVER = "https://api.lever.co/v0/postings/"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/"
U1, U2 = "0b7c1a7e-1111-4a2b-9c3d-000000000001", "0b7c1a7e-1111-4a2b-9c3d-000000000002"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """No sleeping and empty caches in every test."""
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)
    feed._items.clear()
    feed._boards.clear()


def _serve(feed_http, data: dict) -> None:
    """Route jobstories.json and each item to the recorded responses."""
    feed_http[f"{feed.API}/jobstories.json"] = data["jobstories"]
    for it in data["items"]:
        feed_http[f"{feed.API}/item/{it['id']}.json"] = it


def test_parses_recorded_items(feed_http, load_fixture):
    data = json.loads(load_fixture("hn_jobstories.json"))
    _serve(feed_http, data)
    feed_http[ASHBY + "wasmer"] = {
        "jobs": [
            {
                "title": "OS/Hypervisor Experts",
                "id": "a1b2c3d4-0000-4000-8000-000000000001",
                "jobUrl": "https://jobs.ashbyhq.com/wasmer/a1b2c3d4-0000-4000-8000-000000000001",
                "descriptionPlain": "At Wasmer we build the fastest WebAssembly runtime.",
            }
        ]
    }
    feed_http[GH + "subimage"] = {
        "jobs": [
            {
                "title": "Account Executive",
                "id": 1,
                "company_name": "SubImage",
                "absolute_url": "https://job-boards.greenhouse.io/subimage/jobs/1",
            }
        ]
    }
    out = feed.fetch(FeedOptions())
    by = {x.source_ref: x for x in out}
    assert len(out) == 6 and [x.source_ref for x in out] == [f"hn{i}" for i in data["jobstories"]]

    w = by["hn50300643"]  # WaaS link, replaced by the matching Ashby posting
    assert w.company == "Wasmer" and w.ats == "ashby" and w.url.startswith("https://jobs.ashbyhq.com/wasmer/")
    assert w.key == "ashby:a1b2c3d4-0000-4000-8000-000000000001"
    assert w.hints["links"] == "https://www.workatastartup.com/jobs/98722" and w.posted_at == date(2026, 10, 30)

    s = by["hn49563415"]  # YC job page (digit-free YC id); board found but no title match
    assert s.ats == "workatastartup" and s.ats_job_id == "hn49563415" and s.company == "SubImage"
    assert s.url.startswith("https://www.ycombinator.com/companies/subimage/jobs/")
    assert s.hints["links"] == "https://job-boards.greenhouse.io/subimage"

    p = by["hn50078686"]  # direct Lever posting: the ATS id is the identity
    assert p.key == "lever:c7d50a36-db0a-4574-bfe4-8700b74a22c0" and p.ats == "lever" and p.ats_job_id is None

    n = by["hn50280824"]  # careers page without an id: identity is the HN item
    assert n.company == "Nango" and n.ats_job_id == "hn50280824" and n.description == ""
    assert by["hn50126774"].company == "Flexport"

    q = by["hn49524704"]  # text-only post: the HN item is the link
    assert q.url == "https://news.ycombinator.com/item?id=49524704" and q.company == "Quill"
    assert "Fullstack SDK" in q.description or "fullstack SDK" in q.description
    assert "https://quill.co/" in q.hints["links"]

    probed = {str(c.url) for c in feed_http.calls if "posting" in str(c.url) or "boards-api" in str(c.url)}
    assert any("lever.co/v0/postings/wasmer" in u for u in probed)
    assert not any("nango" in u or "flexport" in u for u in probed)  # only YC-login links are probed


def test_cache_since_and_max_results(feed_http, load_fixture):
    data = json.loads(load_fixture("hn_jobstories.json"))
    _serve(feed_http, data)
    out = feed.fetch(FeedOptions(since=date(2026, 10, 25)))
    assert [x.company for x in out] == ["Wasmer", "Nango"]  # stops at the first older post
    before = len(feed_http.calls)
    assert len(feed.fetch(FeedOptions(max_results=2))) == 2
    items_fetched = [c for c in feed_http.calls[before:] if "/item/" in str(c.url)]
    assert items_fetched == []  # both items came from the cache


def test_malformed_and_dead_items_skipped(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [1, 2, 3, 4, 5, "x", 6]
    rows = {
        1: {
            "id": 1,
            "type": "job",
            "time": 1793343635,
            "title": "Acme (YC S20) Is Hiring Engineers",
            "url": "https://acme.example/careers",
        },
        2: {"id": 2, "type": "job", "dead": True, "title": "Dead (YC S20) Is Hiring", "url": "https://d.example/"},
        3: {"id": 3, "type": "story", "title": "Not a job", "url": "https://s.example/"},
        4: {"id": 4, "type": "job", "title": "", "url": "https://e.example/"},
        5: {"id": 5, "type": "job", "title": "Bad (YC W21) Is Hiring", "url": "javascript:alert(1)"},
    }
    for i, r in rows.items():
        feed_http[f"{feed.API}/item/{i}.json"] = r
    feed_http[f"{feed.API}/item/6.json"] = "null"
    out = feed.fetch()
    assert [x.company for x in out] == ["Acme", "Bad"]
    assert out[1].url == "https://news.ycombinator.com/item?id=5"  # unusable url falls back to the HN item


def test_title_hints():
    assert feed._hints("X (YC S20) Is Hiring New Grad Engineers (Remote, US)") == {"remote": "yes", "level": "new_grad"}
    assert feed._hints("X (YC S20) Is Hiring a Senior Engineer, Full-Time")["level"] == "senior"
    assert feed._company_role("Gooseworks (YC W23) Is Hiring – Founding Engineer") == (
        "Gooseworks",
        "Founding Engineer",
    )


def _yc_item(iid: int, title: str, url: str) -> dict:
    """A job story shaped like the API's."""
    return {"id": iid, "type": "job", "time": 1793343635, "title": title, "url": url, "by": "x"}


def test_registered_as_default_list():
    assert feeds.lists_registry()["hn_jobstories"] is feed.FEED and feed.FEED.default
    assert "hn_jobstories" not in feeds.boards_registry()


def test_digit_free_yc_id_falls_back_to_hn_item(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [7, 8]
    feed_http[f"{feed.API}/item/7.json"] = _yc_item(
        7,
        "Hazel (YC W24) Is Hiring a Founding Engineer",
        "https://www.ycombinator.com/companies/hazel-2/jobs/PpNKYBS-founding-engineer",
    )
    feed_http[f"{feed.API}/item/8.json"] = _yc_item(
        8, "Coris (YC S23) Is Hiring", "https://www.ycombinator.com/companies/coris/jobs/h0ndly8-swe"
    )
    out = feed.fetch()
    assert out[0].ats_job_id == "hn7" and out[0].key == "ycombinator.com:hn7"
    assert out[1].ats_job_id == "h0ndly8"


def test_same_named_board_of_another_company_is_not_used(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [9]
    feed_http[f"{feed.API}/item/9.json"] = _yc_item(
        9, "Kyber (YC W23) Is Hiring a Founding Engineer", "https://www.workatastartup.com/jobs/123"
    )
    feed_http[GH + "kyber"] = {
        "jobs": [
            {
                "title": "Founding Engineer",
                "id": 55,
                "company_name": "Kyber Network",
                "absolute_url": "https://job-boards.greenhouse.io/kyber/jobs/55",
            }
        ]
    }
    x = feed.fetch()[0]
    assert x.url == "https://www.workatastartup.com/jobs/123" and x.ats == "workatastartup"
    assert x.hints["links"] == "https://job-boards.greenhouse.io/kyber"  # still discovered, never swapped in


def test_lever_owner_checked_in_posting_text(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [10, 11]
    feed_http[f"{feed.API}/item/10.json"] = _yc_item(
        10, "Roame (YC S22) Is Hiring a Founding Engineer", "https://www.workatastartup.com/jobs/1"
    )
    feed_http[f"{feed.API}/item/11.json"] = _yc_item(
        11, "Roame (YC S22) Is Hiring a Product Designer", "https://www.workatastartup.com/jobs/2"
    )
    feed_http[GH + "roame"] = httpx.Response(404)
    feed_http[LEVER + "roame"] = [
        {
            "text": "Founding Engineer",
            "id": U1,
            "hostedUrl": f"https://jobs.lever.co/roame/{U1}",
            "descriptionPlain": "Roame helps travelers book award flights.",
        },
        {
            "text": "Product Designer",
            "id": U2,
            "hostedUrl": f"https://jobs.lever.co/roame/{U2}",
            "descriptionPlain": "Join a travel company.",
        },
    ]
    a, b = feed.fetch()
    assert a.url == f"https://jobs.lever.co/roame/{U1}" and a.ats == "lever" and a.key == f"lever:{U1}"
    assert b.ats == "workatastartup" and "https://jobs.lever.co/roame" in b.hints["links"]


def test_board_link_appended_to_existing_links():
    x = Listing(
        company="Cua",
        title="Cua (YC X25) Is Hiring Engineers",
        url="https://www.workatastartup.com/jobs/5",
        ats="workatastartup",
        hints={"links": "https://cua.ai/careers https://cua.ai/jobs"},
    )
    y = feed._prefer_ats(x, ("greenhouse", "cua", []))
    assert y.hints["links"] == "https://cua.ai/careers https://cua.ai/jobs https://job-boards.greenhouse.io/cua"
    full = x.model_copy(update={"hints": {"links": " ".join(f"https://cua.ai/{'x' * 40}{i}" for i in range(6))}})
    assert len(feed._prefer_ats(full, ("greenhouse", "cua", [])).hints["links"]) <= 300


def test_only_404_is_cached(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [13]
    feed_http[f"{feed.API}/item/13.json"] = _yc_item(
        13, "Bild (YC W25) Is Hiring", "https://www.workatastartup.com/jobs/9"
    )
    feed_http[GH + "bild"] = httpx.Response(503)
    feed.fetch()
    assert "bild" not in feed._boards  # a 5xx is retried next time
    feed_http[GH + "bild"] = httpx.Response(404)
    feed_http[LEVER + "bild"] = httpx.Response(404)
    feed_http[ASHBY + "bild"] = httpx.Response(404)
    feed.fetch()
    assert feed._boards["bild"] is None  # three 404s: no board, remembered


def test_describe_uses_ats_api_only_for_moved_postings(feed_http):
    feed_http["https://boards-api.greenhouse.io/v1/boards/acme/jobs/42"] = {
        "id": 42,
        "title": "SWE",
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/42",
        "content": "&lt;p&gt;Build things.&lt;/p&gt;",
        "location": {"name": "SF"},
    }
    moved = Listing(company="Acme", title="SWE", url="https://job-boards.greenhouse.io/acme/jobs/42", ats="greenhouse")
    assert "Build things." in feed.describe(moved)
    yc = Listing(company="Acme", title="SWE", url="https://www.workatastartup.com/jobs/1", ats="workatastartup")
    before = len(feed_http.calls)
    assert feed.describe(yc) == "" and len(feed_http.calls) == before


def test_waas_detection_is_exact():
    assert feed._needs_yc_login("https://www.workatastartup.com/jobs/1")
    assert feed._needs_yc_login("https://www.ycombinator.com/companies/acme/jobs/AbC123x-swe")
    assert not feed._needs_yc_login("https://evilworkatastartup.com/jobs/1")
    assert not feed._needs_yc_login("https://www.ycombinator.com/companies/acme")


def test_direct_greenhouse_link_gets_ats_and_text(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [21]
    feed_http[f"{feed.API}/item/21.json"] = _yc_item(
        21,
        "Piramidal (YC W24) Is Hiring a Founding Engineer",
        "https://job-boards.greenhouse.io/piramidalinc/jobs/4443102009",
    )
    feed_http[GH + "piramidalinc/jobs/4443102009"] = {
        "id": 4443102009,
        "title": "Founding Engineer",
        "content": "&lt;p&gt;Brain foundation models.&lt;/p&gt;",
        "absolute_url": "https://job-boards.greenhouse.io/piramidalinc/jobs/4443102009",
        "location": {"name": "SF"},
    }
    (x,) = feed.fetch()
    assert x.ats == "greenhouse" and x.key == "greenhouse:4443102009"
    assert "Brain foundation models." in feed.describe(x)
    legacy = x.model_copy(update={"ats": "unknown"})  # describe goes by the link, not the stored ats
    assert "Brain foundation models." in feed.describe(legacy)


def test_board_link_without_posting_goes_to_hints(feed_http):
    feed_http[f"{feed.API}/jobstories.json"] = [22]
    feed_http[f"{feed.API}/item/22.json"] = _yc_item(
        22, "Weave (YC W25) Is Hiring", "https://jobs.ashbyhq.com/weave-os"
    )
    (x,) = feed.fetch()
    assert x.ats == "unknown" and x.ats_job_id == "hn22"
    assert x.hints["links"] == "https://jobs.ashbyhq.com/weave-os"
    before = len(feed_http.calls)
    assert feed.describe(x) == "" and len(feed_http.calls) == before


def test_ats_ref_requires_the_real_ats_host():
    assert feed._ats_ref("https://jobs.lever.co/postera/c7d50a36-db0a-4574-bfe4-8700b74a22c0").ats == "lever"
    assert feed._ats_ref("https://evil.com/?u=jobs.lever.co/postera/c7d50a36-db0a-4574-bfe4-8700b74a22c0") is None
    assert feed._ats_ref("https://evilgreenhouse.io/x/jobs/1") is None
    assert feed._ats_ref("https://acme.com/careers?gh_jid=123") is None  # no slug: nothing to fetch
    assert feed._ats_ref("https://nango.dev/careers") is None
