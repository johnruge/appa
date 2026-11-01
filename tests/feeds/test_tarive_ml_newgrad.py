"""Tar-ive ML_Newgrad_2027 meta-list, from a recorded slice of the real file."""

from __future__ import annotations

import json
from datetime import date

from appa.feeds import FeedOptions
from appa.feeds.lists import tarive_ml_newgrad as feed


def test_reads_active_us_rows(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("tarive_ml_newgrad.json")
    out = feed.fetch(FeedOptions())
    by_company = {}
    for x in out:
        by_company.setdefault(x.company, []).append(x)
    assert "ICF" not in by_company and "Caterpillar" not in by_company
    zoom = by_company["Zoom"][0]
    assert zoom.url.startswith("https://zoom.wd5.myworkdayjobs.com/") and zoom.ats == "workday"
    assert zoom.posted_at == date(2026, 10, 30) and zoom.source_ref == "f6f13c7ed8a5"
    assert zoom.hints == {"category": "AI/ML/Data", "level": "new_grad", "country": "US"}
    # two distinct SambaNova requisitions (gh_jid), the trailing-slash variant and dreamworkhq link dropped
    samba = by_company["SambaNova Systems"]
    assert sorted(x.ats_job_id for x in samba) == ["6224936004", "6224938004"]
    assert all("dreamworkhq" not in x.url for x in samba) and samba[0].hints["links"].startswith(
        "https://sambanova.ai/"
    )
    # direct apply_url wins over the aggregator links in urls[]
    assert [x.url for x in by_company["Booz Allen Hamilton"]] == [
        "https://bah.wd1.myworkdayjobs.com/BAH_Jobs/job/Washington-DC/Data-Scientist--Junior_R0252270"
    ]
    # only an aggregator link and no date_posted: kept, dated by first_seen
    amzn = by_company["Amazon.com Services LLC"][0]
    assert amzn.url.startswith("https://zapply.jobs/") and amzn.posted_at == date(2026, 10, 15)
    addepar = by_company["Addepar"][0]
    assert addepar.ats == "greenhouse" and addepar.ats_job_id == "8906234002"
    assert addepar.hints["salary"] == "$163,000 - $203,500" and addepar.hints["remote"] == "yes"
    assert "level" not in addepar.hints  # "1-3 yrs": seniority unstated
    # the file's internal tag "ai_first" is not a role category: fall back to the family
    assert by_company["Booz Allen Hamilton"][0].hints["category"] == "Data Science"
    assert by_company["Raydar"][0].hints["level"] == "mid"  # "2-6 yrs"
    # Amazon login-gated apply links become public pages; 10522632 (also listed by another row) collapses
    amazon = by_company["Amazon"]
    assert sorted(x.ats_job_id for x in amazon) == ["10476858", "10522632", "10524924"]
    assert all("account.amazon.jobs" not in x.url for x in amazon)
    assert "https://www.amazon.jobs/en/jobs/10524924" in {x.url for x in amazon}
    assert [x.url for x in by_company["Fab2"]] == ["https://jobs.ashbyhq.com/fab2/d9edb922-6cae-4e65-9b20-ba452f3e2edf"]
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    assert len(json.loads(load_fixture("tarive_ml_newgrad.json"))) == 12 and len(out) == 11


def test_canonical_links():
    assert (
        feed._canonical("https://account.amazon.jobs/jobs/10584405/apply") == "https://www.amazon.jobs/en/jobs/10584405"
    )
    assert feed._canonical("https://account.amazon.jobs/jobs/10584405/") == "https://www.amazon.jobs/en/jobs/10584405"
    assert feed._canonical("https://account.amazon.jobs.evil.com/jobs/1/apply").startswith(
        "https://account.amazon.jobs.evil"
    )
    u = "https://jobs.ashbyhq.com/quora/0b1c2d3e-aaaa-bbbb-cccc-111122223333"
    assert feed._canonical(u + "/application?embed=true") == u and feed._canonical(u + "/application") == u
    assert feed._canonical("https://evil.com/x/application?embed=true") == "https://evil.com/x/application?embed=true"


def test_since_dedupe_and_malformed_rows(feed_http):
    base = {"active": True, "is_usa": True, "title": "ML Engineer", "locations": ["NYC"]}
    feed_http[feed.URL] = [
        {
            **base,
            "id": "a",
            "company": "A",
            "url": "https://boards.greenhouse.io/a/jobs/111",
            "date_posted": 1767225600,
        },  # 2026-01-01
        {
            **base,
            "id": "a2",
            "company": "A",
            "url": "https://job-boards.greenhouse.io/a/jobs/111",
            "date_posted": 1767225600,
        },  # same posting
        {**base, "id": "b", "company": "B", "url": "https://b.example/jobs/222", "date_posted": 1735689600},
        {**base, "id": "c", "company": "C", "url": "javascript:alert(1)", "urls": ["ftp://x/1"]},
        {**base, "id": "d", "company": "", "url": "https://d.example/jobs/3"},
        {
            **base,
            "id": "e",
            "company": "E",
            "url": "https://e.example/jobs/4",
            "verification": {"status": "too_senior"},
        },
        {**base, "id": "f", "company": "F", "url": "https://f.example/jobs/5", "is_usa": False},
        "not a row",
        None,
    ]
    out = feed.fetch(FeedOptions(since=date(2025, 6, 1)))
    assert [x.company for x in out] == ["A", "E"]
    assert out[1].hints["level"] == "mid"
    assert feed.fetch(FeedOptions(max_results=1))[0].company == "A"


def test_aggregator_hosts_are_exact():
    assert feed._aggregator("https://www.dreamworkhq.com/job/x") and feed._aggregator("https://zapply.jobs/l/d/x")
    assert not feed._aggregator("https://evilzapply.jobs/x") and not feed._aggregator("https://zapply.jobs.evil.com/x")
