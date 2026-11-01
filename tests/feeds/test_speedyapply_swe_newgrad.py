"""SpeedyApply 2027 new-grad lists (SWE and AI/ML), from a recorded slice of the real Markdown file."""

from __future__ import annotations

from datetime import date, timedelta

from appa.feeds import FeedOptions
from appa.feeds.lists import speedyapply_swe_newgrad as feed

HEAD = "<!-- TABLE_START -->\n| Company | Position | Location | Posting | Age |\n|---|---|---|---|---|\n"
APPLY = '<a href="{}"><img src="https://i.imgur.com/x.png" alt="Apply"/></a>'


def _row(company: str, title: str, url: str, age: str = "1d", loc: str = "NYC") -> str:
    """One row of the Other table, in the file's format."""
    return f'| <a href="https://c.example"><strong>{company}</strong></a> | {title} | {loc} | {APPLY.format(url)} | {age} |\n'


def test_reads_all_three_tables(feed_http, load_fixture):
    feed_http[feed.SWE_URL] = load_fixture("speedyapply_swe_newgrad.md")
    out = feed.fetch(FeedOptions())
    assert len(out) == 6
    by = {x.company: x for x in out}
    assert set(by) == {"Amazon", "Roblox", "Citadel", "HelloFresh", "Blue Yonder", "Guidehouse"}
    amazon = by["Amazon"]
    assert amazon.url == "https://www.amazon.jobs/jobs/10592978/apply" and amazon.ats_job_id == "10592978"
    assert amazon.hints["salary"] == "$186k/yr" and amazon.hints["category"] == "SWE / FAANG+"
    assert amazon.hints["level"] == "new_grad" and amazon.hints["country"] == "US"
    assert amazon.posted_at == date.today() - timedelta(days=3)
    assert by["Roblox"].ats == "greenhouse" and by["Roblox"].ats_job_id == "8259453"
    assert by["Citadel"].hints["category"] == "SWE / Quant" and by["Citadel"].location == "Houston, TX"
    other = by["HelloFresh"]
    assert other.hints["category"] == "SWE / Other" and "salary" not in other.hints  # no Salary column there
    assert by["Blue Yonder"].hints["citizenship_required"] == "yes"
    assert by["Guidehouse"].hints["remote"] == "yes"
    assert all(x.url.startswith("https://") and x.description == "" for x in out)
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)


def test_ai_feed_uses_its_own_file(feed_http, load_fixture):
    feed_http[feed.AI_URL] = load_fixture("speedyapply_swe_newgrad.md")
    out = feed.fetch_ai(FeedOptions(max_results=2))
    assert len(out) == 2 and all(x.hints["category"].startswith("AI/ML / ") for x in out)
    assert [str(c.url) for c in feed_http.calls] == [feed.AI_URL]


def test_malformed_rows_since_and_duplicates(feed_http):
    body = HEAD + _row("A", "SWE", "https://a.example/jobs/1", "2d") + _row("Old", "SWE", "https://o.example/1", "400d")
    body += _row("A", "SWE", "https://a.example/jobs/1", "2d")  # same link twice
    body += _row("C", "SWE", "javascript:alert(1)")
    body += _row("", "SWE", "https://d.example/1")
    body += '| <a href="https://e.example"><strong>E</strong></a> | SWE | NYC | 1d |\n'  # missing a cell
    body += "| <a> | not | a | real | row |\n<!-- TABLE_END -->\n"
    feed_http[feed.SWE_URL] = body
    out = feed.fetch(FeedOptions(since=date.today() - timedelta(days=30)))
    assert [x.company for x in out] == ["A"]


def test_file_without_tables_is_an_error(feed_http):
    feed_http[feed.SWE_URL] = "# moved\nnothing here\n"
    try:
        feed.fetch(FeedOptions())
    except ValueError:
        return
    raise AssertionError("expected ValueError")
