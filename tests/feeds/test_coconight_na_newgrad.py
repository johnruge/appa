"""coconight01 2027 North America new-grad list, from a recorded slice of the real jobs.json."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa.feeds import HINT_KEYS, FeedOptions
from appa.feeds.lists import coconight_na_newgrad as feed


def test_reads_open_rows_with_ids_and_hints(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("coconight_na_newgrad.json")
    out = feed.fetch(FeedOptions())
    rows = json.loads(load_fixture("coconight_na_newgrad.json"))
    assert len(out) == len(rows) - 2  # the Closed row and the Workday zapply row are dropped
    assert not {"Brain Co.", "NVIDIA"} & {x.company for x in out}
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    assert all(set(x.hints) <= set(HINT_KEYS) for x in out)
    by_url = {x.url: x for x in out}

    db = out[0]  # newest: Databricks, 2026-10-28
    assert db.company == "Databricks" and db.posted_at == date(2026, 10, 28)
    assert db.title == "Software Engineer - New Grad, Distributed Data Systems (2027 Start)"
    assert db.ats_job_id == "6865704002" and db.key == "databricks.com:6865704002"
    assert db.hints["salary"] == "$145,000-$155,000/yr" and db.hints["degrees"] == "BS/MS"
    assert db.hints["level"] == "new_grad" and "sponsorship" not in db.hints  # "Unknown" is not a label
    assert db.location == "Bellevue, WA; Mountain View, CA" and db.hints["country"] == "United States"

    z = by_url["https://job-boards.greenhouse.io/andurilindustries/jobs/5262340007"]  # was a zapply link
    assert z.ats == "greenhouse" and z.ats_job_id == "5262340007"
    assert z.hints["links"] == "https://zapply.jobs/l/d/greenhouse-andurilindustries-5262340007?s=gh-new-grad-jobs-2027"
    assert not any("zapply.jobs" in x.url for x in out)

    gh = by_url["https://boards.greenhouse.io/andurilindustries/jobs/5262340007?gh_jid=5262340007"]
    assert gh.ats == "greenhouse" and gh.ats_job_id == "5262340007" and gh.key == "greenhouse:5262340007"
    assert z.key == gh.key  # the zapply row dedupes with the board row

    bd = by_url["https://jobs.bytedance.com/en/position/7673626147481291061/detail"]
    assert bd.company == "ByteDance" and bd.ats == "bytedance" and bd.ats_job_id == "7673626147481291061"
    assert bd.hints["level"] == "new_grad" and "zapply.jobs/l/d/bytedance-" in bd.hints["links"]

    nbc = by_url["https://jobs.smartrecruiters.com/NBCUniversal3/744000154453160"]  # from an internships list
    assert nbc.ats == "smartrecruiters" and nbc.ats_job_id == "sr-NBCUniversal3-744000154453160"
    assert "level" not in nbc.hints and "employment_type" not in nbc.hints

    jr = by_url["https://jobright.ai/jobs/info/6aaecb182e757fcb5c8bbfe6"]
    assert jr.key == "jobright.ai:6aaecb182e757fcb5c8bbfe6"

    sig = next(x for x in out if x.company == "Susquehanna International Group")
    assert sig.hints["sponsorship"] == "Yes" and "citizenship_required" not in sig.hints


def test_since_flags_and_malformed_rows(feed_http):
    base = {"status": "Open", "role": "SWE", "posted_date": "2026-10-01"}
    feed_http[feed.URL] = [
        {
            **base,
            "company": "A",
            "url": "https://a.example/jobs/12345",
            "citizenship_required": "Yes",
            "location": "Remote, US",
            "salary_min_annual": "120000",
            "salary_max_annual": "",
            "ng_confidence": "Uncertain",
        },
        {**base, "company": "B", "url": "https://b.example/1", "posted_date": "2025-01-01"},
        {**base, "company": "C", "url": "javascript:alert(1)"},
        {**base, "company": "", "url": "https://d.example/1"},
        {**base, "company": "E", "url": "https://e.example/1", "role": ""},
        {**base, "company": "F", "url": "https://f.example/1", "status": "Closed"},
        "not a row",
        {**base, "company": "W", "url": "https://apply.workable.com/raydar/j/A876C6B170/apply"},
        {**base, "company": "L", "url": "https://zapply.jobs/l/d/lever-acme-0a1b2c3d-1111-2222-3333-444455556666"},
        {**base, "company": "K", "url": "https://zapply.jobs/l/d/workday-acme-R12345?s=x"},
        {**base, "company": "O", "url": "https://zapply.jobs/l/d/oracle-acme-210803936"},
        {**base, "company": "Z", "url": "https://zapply.jobs/l/d/greenhouse-..-12345"},
        {**base, "company": "Q", "url": "https://zapply.jobs/l/d/google-12345678x"},
        {
            **base,
            "company": "G",
            "url": "https://g.example/1",
            "posted_date": "garbage",
            "date_added": "2026-09-01",
            "phd_required": "Yes",
            "salary": "Not listed",
        },
    ]
    out = feed.fetch(FeedOptions(since=date(2026, 6, 1)))
    assert [x.company for x in out] == ["A", "W", "L", "G"]  # zapply links that lead nowhere are dropped
    a, w, lv, g = out
    assert lv.url == "https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666" and lv.ats == "lever"
    assert lv.ats_job_id == "0a1b2c3d-1111-2222-3333-444455556666" and "zapply.jobs" in lv.hints["links"]
    assert w.ats == "workable" and w.key == "apply.workable.com:a876c6b170"
    assert a.hints["citizenship_required"] == "yes" and a.hints["remote"] == "yes"
    assert a.hints["salary"] == "$120,000/yr" and "level" not in a.hints
    assert g.posted_at == date(2026, 9, 1) and g.hints["degrees"] == "PhD" and "salary" not in g.hints


@pytest.mark.parametrize(
    "slug,ats,jid,link",
    [
        (
            "google-123087196289671878",
            "google",
            "123087196289671878",
            "https://www.google.com/about/careers/applications/jobs/results/123087196289671878",
        ),
        (
            "gem-nominal-am9icG9zdDrespQGgGw-h1yZcUDntmT2",
            "gem",
            "gem-nominal-am9icG9zdDrespQGgGw-h1yZcUDntmT2",
            "https://jobs.gem.com/nominal/am9icG9zdDrespQGgGw-h1yZcUDntmT2",
        ),
        (
            "sr-AbbVie-3743990015545356",
            "smartrecruiters",
            "sr-AbbVie-3743990015545356",
            "https://jobs.smartrecruiters.com/AbbVie/3743990015545356",
        ),
        (
            "ashby-my-co-0a1b2c3d-1111-2222-3333-444455556666",
            "ashby",
            "0a1b2c3d-1111-2222-3333-444455556666",
            "https://jobs.ashbyhq.com/my-co/0a1b2c3d-1111-2222-3333-444455556666",
        ),
    ],
)
def test_zapply_slugs_to_direct_links(slug, ats, jid, link):
    assert feed._zapply(slug) == (ats, jid, link)


@pytest.mark.parametrize(
    "slug",
    [
        "workday-ngc-northrop-grumman-external-site-R10254896",
        "oracle-jpmc-210803936",
        "amazon-5ff0199f-83f5-4139-be12-3dc4f8b52bf8",
        "greenhouse-.x-123456",
        "sr--123456",
        "bytedance-12ab",
        "google-",
        "gem-acme-notbase64",
        "nothing",
    ],
)
def test_zapply_slugs_without_a_posting_link(slug):
    assert feed._zapply(slug) is None


def test_intern_and_summer_levels(feed_http):
    base = {"status": "Open", "company": "A", "ng_confidence": "Confirmed", "posted_date": "2026-10-01"}
    feed_http[feed.URL] = [
        {**base, "role": "Software Engineer Intern", "url": "https://a.example/1"},
        {**base, "role": "2027 Summer Analyst", "url": "https://a.example/2"},
        {**base, "role": "Rotational Program - Summer 2027", "url": "https://a.example/3"},
        {**base, "role": "Engineer - New Grad (Summer 2027)", "url": "https://a.example/4"},
        {
            **base,
            "role": "New Grad Construction Rep I - Summer 2027",
            "url": "https://zapply.jobs/l/d/greenhouse-hntb-1234567?s=gh-internships-2027",
        },
    ]
    levels = {x.title: (x.hints.get("level"), x.hints.get("employment_type")) for x in feed.fetch(FeedOptions())}
    assert levels == {
        "Software Engineer Intern": ("intern", "internship"),
        "2027 Summer Analyst": ("intern", "internship"),
        "Rotational Program - Summer 2027": (None, None),
        "Engineer - New Grad (Summer 2027)": ("new_grad", "full_time"),
        "New Grad Construction Rep I - Summer 2027": (None, None),
    }


def test_max_results_and_errors(feed_http):
    feed_http[feed.URL] = [
        {"status": "Open", "company": f"C{i}", "role": "SWE", "url": f"https://c.example/{i}"} for i in range(10)
    ]
    assert len(feed.fetch(FeedOptions(max_results=3))) == 3
    feed_http[feed.URL] = {"jobs": []}
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())
    feed_http[feed.URL] = httpx.Response(500, content=b"boom")
    with pytest.raises(httpx.HTTPStatusError):
        feed.fetch(FeedOptions())


def test_registered():
    from appa.feeds import lists_registry

    assert lists_registry()["coconight_na_newgrad"].fetch is feed.fetch
