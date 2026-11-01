"""DereC4 internships-and-newgrad list, from a recorded slice of the real NEWGRAD.md."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import derec4_newgrad as feed

TODAY = datetime.now(UTC).date()


def _file(*rows: str) -> str:
    """A NEWGRAD.md with just these table rows."""
    return (
        f"{feed.START}\n\n| Company | Role | Location | Age |\n| --- | --- | --- | --- |\n"
        + "".join(r + "\n" for r in rows)
        + f"\n{feed.END}\n"
    )


def test_parses_fixture_rows(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("derec4_newgrad.md")
    out = feed.fetch(FeedOptions())
    by = {x.company: x for x in out}
    # interns (Yotta, Toyota) and the 2mo row (9to9) are dropped
    assert set(by) == {"Amentum", "Roblox", "Agave", "CrossCountry Consulting", "TikTok", "Adobe"}
    assert all(x.hints["level"] == "new_grad" for x in out)
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)

    rb = by["Roblox"]
    assert rb.title == "[2027] Senior Software Engineer - Data Acquisition PhD Early Career"
    assert rb.url == "https://job-boards.greenhouse.io/roblox/jobs/8315883"
    assert rb.ats == "greenhouse" and rb.ats_job_id == "8315883" and rb.posted_at == TODAY
    assert rb.hints["links"] == "https://zapply.jobs/l/d/greenhouse-roblox-8315883"

    am = by["Amentum"]
    assert am.url == "https://zapply.jobs/l/d/workday-pae-amentum-careers-R0173771"
    assert am.ats == "workday" and am.ats_job_id == "workday-pae-amentum-careers-R0173771"
    assert am.hints["citizenship_required"] == "yes" and "links" not in am.hints

    ag = by["Agave"]
    assert ag.url.startswith("https://jobs.ashbyhq.com/agave/73bd2c98-") and ag.ats == "ashby"
    assert ag.ats_job_id == "73bd2c98-bf87-4eaa-aea8-35e7b6131d0e" and ag.posted_at == TODAY - timedelta(days=3)
    assert by["CrossCountry Consulting"].ats == "lever"
    assert by["TikTok"].ats == "unknown" and by["TikTok"].ats_job_id == "7680761598014015797"
    assert by["Adobe"].posted_at == TODAY - timedelta(days=30)


def test_age_since_dedupe_and_malformed(feed_http):
    old = (TODAY - timedelta(days=40)).isoformat()
    feed_http[feed.URL] = _file(
        "| A | [SWE](https://a.example/jobs/101) | NYC | 2d |",
        "| A | [SWE again](https://a.example/jobs/101) | NYC | 1d |",  # same posting key
        "| B | [SWE](https://b.example/jobs/202) | Remote | 10d |",
        "| C | [SWE](https://c.example/jobs/303) | NYC | Date unknown |",
        f"| D | [SWE](https://d.example/jobs/404) | NYC | {old} |",
        "| E | [SWE](javascript:alert(1)) | NYC | 0d |",
        "|  | [SWE](https://f.example/jobs/505) | NYC | 0d |",
        "| G | SWE https://g.example/jobs/606 | NYC | 0d |",
        "| H | [Software Engineer Intern](https://h.example/jobs/707) | NYC | 0d |",
        "| I | [SWE](https://i.example/jobs/808) | NYC |",
    )
    out = feed.fetch(FeedOptions())
    assert [x.company for x in out] == ["A", "B"] and out[0].title == "SWE again"
    assert out[1].hints["remote"] == "yes"
    assert [x.company for x in feed.fetch(FeedOptions(since=TODAY - timedelta(days=5)))] == ["A"]
    assert len(feed.fetch(FeedOptions(max_results=1))) == 1


def test_missing_table_raises(feed_http):
    feed_http[feed.URL] = "# nothing here\n"
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())
