"""Zapply New-Grad 2027 READMEs, from a recorded slice of the real SWE README."""

from __future__ import annotations

from datetime import UTC, date, datetime

from appa.feeds import FeedOptions
from appa.feeds.lists import zapply_newgrad2027 as feed

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
ROW = (
    "| **{c}** | {t} | {loc} | {age} | {visa} | "
    "[<img src=\"images/apply.png\" width=\"80\" alt=\"Apply\">](https://zapply.jobs/l/d/{slug}?s=gh-x) |"
)


def _readme(*rows: str, section: str = "Software Engineering") -> str:
    """A minimal README with one section holding these rows."""
    head = (
        f"<details>\n<summary><h3>💻 <strong>{section}</strong></h3></summary>\n\n"
        "| Company | Role | Location | Posted | Visa | **Apply** |\n|---|---|---|---|---|---|\n"
    )
    return head + "\n".join(rows) + "\n\n</details>\n"


def test_parses_fixture_rows(load_fixture):
    out = feed.parse(load_fixture("zapply_newgrad2027.html").decode(), NOW)
    by = {x.company: x for x in out}
    assert set(by) == {"NVIDIA", "Roblox", "Northwood Space", "Veolia Environnement SA", "ByteDance", "Schonfeld"}

    nv = by["NVIDIA"]  # internships are tagged, not dropped
    assert nv.hints["level"] == "intern" and nv.hints["employment_type"] == "internship"
    assert nv.ats == "workday" and nv.hints["sponsorship"] == "Sponsor (Zapply estimate)"

    rb = by["Roblox"]
    assert rb.title == "[2027] Senior Software Engineer - Data Acquisition PhD Early Career"  # markdown escapes gone
    assert rb.url == "https://job-boards.greenhouse.io/roblox/jobs/8315883"
    assert (rb.ats, rb.ats_job_id, rb.key) == ("greenhouse", "8315883", "greenhouse:8315883")
    assert rb.hints["links"] == "https://zapply.jobs/l/d/greenhouse-roblox-8315883"
    assert rb.hints["sponsorship"] == "Sponsor (Zapply estimate)"
    assert rb.posted_at == TODAY and rb.source_ref == "greenhouse-roblox-8315883"

    nw = by["Northwood Space"]
    assert nw.url == "https://jobs.ashbyhq.com/northwoodspace/f7b9e8d5-7368-4a54-b445-ff647f12b1b6"
    assert nw.ats == "ashby" and nw.posted_at == date(2026, 10, 29) and "sponsorship" not in nw.hints

    sr = by["Veolia Environnement SA"]
    assert sr.url == "https://zapply.jobs/l/d/sr-VeoliaEnvironnementSA-744000158336900"
    assert sr.ats == "smartrecruiters" and sr.ats_job_id == "sr-VeoliaEnvironnementSA-744000158336900"
    assert sr.posted_at == date(2026, 10, 28) and "links" not in sr.hints

    assert by["ByteDance"].posted_at is None and by["ByteDance"].hints["category"] == "AI / ML Engineering"
    assert by["Schonfeld"].posted_at == date(2026, 4, 30)
    assert all(x.hints["level"] == "new_grad" and "employment_type" not in x.hints for x in out if x is not nv)
    assert all(x.url.startswith("https://") for x in out)


def test_relative_ages():
    assert feed._posted("17m", NOW) == TODAY
    assert feed._posted("5h", NOW) == TODAY
    assert feed._posted("21h", NOW) == date(2026, 10, 29)  # 21 hours before 09:00 is yesterday
    assert feed._posted("3d", NOW) == date(2026, 10, 27)
    assert feed._posted("2w", NOW) == date(2026, 10, 16)
    assert feed._posted("1mo", NOW) == date(2026, 9, 30)
    assert feed._posted("2026-04-30", NOW) == date(2026, 4, 30)
    assert feed._posted("Date unknown", NOW) is None


def test_intern_tagging_and_visa_labels():
    text = _readme(
        ROW.format(c="A", t="Software Engineer Co Op", loc="NYC", age="1d", visa="", slug="workday-a-s-1"),
        ROW.format(c="B", t="Software Engineer Co\u2011Op", loc="NYC", age="1d", visa="", slug="workday-b-s-2"),
        ROW.format(c="C", t="Software Engineering Internship", loc="NYC", age="1d", visa="", slug="workday-c-s-3"),
        ROW.format(
            c="D",
            t="Cybersecurity and Privacy Associate - Summer 2027",
            loc="NYC",
            age="1d",
            visa="No Sponsor",
            slug="workday-d-s-4",
        ),
        ROW.format(c="E", t="Cooperative Systems Engineer", loc="Remote, US", age="1d", visa="", slug="workday-e-s-5"),
        ROW.format(c="F", t="Internal Tools Engineer", loc="NYC", age="1d", visa="", slug="workday-f-s-6"),
    )
    by = {x.company: x for x in feed.parse(text, NOW)}
    assert all(by[c].hints["level"] == "intern" and by[c].hints["employment_type"] == "internship" for c in "ABC")
    for c in "DEF":  # 'Summer' alone, cooperative, internal
        assert by[c].hints["level"] == "new_grad" and "employment_type" not in by[c].hints
    assert by["D"].hints["sponsorship"] == "No Sponsor (Zapply estimate)"  # verbatim, never read as positive
    assert "sponsorship" not in by["A"].hints and by["E"].hints["remote"] == "yes"


def test_fetch_dedupes_across_repos_since_and_sorts(feed_http, load_fixture, monkeypatch):
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)
    feed_http[feed.URLS[0]] = load_fixture("zapply_newgrad2027.html")
    feed_http[feed.URLS[1]] = _readme(
        ROW.format(
            c="Roblox", t="Software Engineer", loc="San Mateo", age="2d", visa="", slug="greenhouse-roblox-8315883"
        ),
        ROW.format(c="Acme", t="Software Engineer I", loc="Austin", age="1d", visa="", slug="workday-acme-site-R1"),
        section="Data, AI & Research",
    )
    out = feed.fetch(FeedOptions())
    assert len(feed_http.calls) == 2
    assert [x.company for x in out].count("Roblox") == 1  # same slug in both repos
    assert "Acme" in {x.company for x in out}
    dated = [x.posted_at for x in out if x.posted_at]
    assert dated == sorted(dated, reverse=True) and out[-1].posted_at is None

    since = feed.fetch(FeedOptions(since=date(2026, 10, 1), max_results=3))
    assert len(since) == 3 and all(x.posted_at is None or x.posted_at >= date(2026, 10, 1) for x in since)


def test_malformed_and_hostile_rows_skipped():
    text = _readme(
        ROW.format(c="", t="SWE", loc="NYC", age="1d", visa="", slug="workday-a-b-1"),  # no company
        ROW.format(c="B", t="", loc="NYC", age="1d", visa="", slug="workday-b-b-2"),  # no title
        "| **C** | SWE | NYC | 1d |  | [Apply](https://evil.example/l/d/workday-c-c-3) |",  # not zapply
        "| **D** | SWE | NYC | 1d |  | [Apply](https://zapply.jobs/l/d/../../etc) |",  # bad slug
        "| **E** | SWE | NYC |",  # short row
        ROW.format(c="F", t="Software Engineer Intern", loc="NYC", age="1d", visa="", slug="lever-f-1"),  # intern, kept
        ROW.format(c="G", t="SWE - TS/SCI", loc="VA", age="1d", visa="✅ Sponsor", slug="ashby-g-not-a-uuid"),
        ROW.format(c="H", t="Software Engineer", loc="NYC", age="1d", visa="✅ Sponsor", slug="greenhouse-a%2Fb-12345"),
    )
    out = feed.parse(text, NOW)
    assert [x.company for x in out] == ["F", "G"]
    assert out[0].hints["level"] == "intern"
    g = out[1]
    assert g.hints["citizenship_required"] == "yes" and "sponsorship" not in g.hints
    assert g.ats == "unknown" and g.url == "https://zapply.jobs/l/d/ashby-g-not-a-uuid"  # unparsable ashby slug
