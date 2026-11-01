"""ResuMax new-grad-tech-jobs list, from a recorded slice of the real README."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from appa.feeds import FeedOptions
from appa.feeds.lists import resumax_newgrad_tech as feed

HEAD = "## Software Engineering\n\n| Company | Role | Location | Apply | Tailor | Age |\n|---|---|---|:--:|:--:|--:|\n"


def _row(company: str, title: str, url: str, age: str = "1d", loc: str = "Austin, TX, us") -> str:
    """One table row, in the README's format."""
    return (
        f"| **[{company}](https://resumax.ai/companies/x)** 🆕 | {title} | {loc} | [Apply]({url}) | "
        f'<a href="https://resumax.ai/tailor?role=x-1&utm_source=github"><img alt="Tailor"></a> | {age} |\n'
    )


def test_reads_us_rows_with_hints(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("resumax_newgrad_tech.md")
    out = feed.fetch(FeedOptions())
    by = {x.company: x for x in out}
    assert set(by) == {"Raydar", "Clera", "Anthropic", "Ennoble Care"}  # NielsenIQ (in) and Dropbox (Mexico) dropped
    today = date.today()
    raydar = by["Raydar"]
    assert raydar.url == "https://apply.workable.com/j/70A2610DAE/apply" and raydar.ats_job_id == "70A2610DAE"
    assert raydar.hints == {"level": "new_grad", "category": "Software Engineering"}
    assert raydar.posted_at == today - timedelta(days=1)
    assert raydar.source_ref == "raydar-backend-software-engineer-7d9a18111044bb33"
    clera = by["Clera"]
    assert clera.ats == "ashby" and clera.ats_job_id == "beee9474-165a-4791-9315-de94249f2c9b"
    ant = by["Anthropic"]
    assert ant.location == "Remote-Friendly (Travel Required) | San Francisco, CA"  # escaped pipe stays in the cell
    assert ant.ats == "greenhouse" and ant.ats_job_id == "5462966008"
    assert ant.hints["remote"] == "yes" and ant.hints["category"] == "Security"
    assert by["Ennoble Care"].hints["category"] == "Hardware & Embedded"
    assert all(x.description == "" and x.url.startswith("https://") for x in out)
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)


def test_global_when_not_us_only(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("resumax_newgrad_tech.md")
    out = feed.fetch(FeedOptions(us_only=False, max_results=100))
    by = {x.company: x for x in out}
    assert len(out) == 6 and by["NielsenIQ"].ats_job_id == "744000158727519"
    assert by["Dropbox"].ats_job_id == "7752226" and by["Dropbox"].hints["remote"] == "yes"
    assert len(feed.fetch(FeedOptions(us_only=False, max_results=2))) == 2


@pytest.mark.parametrize(
    "loc",
    [
        "Austin, TX",
        "Pune, MH, us",
        "San Francisco",
        "Remote",
        "US-CA-Menlo Park",
        "Tysons, Virginia",
        "Remote (United States | Canada)",
        "London; New York, NY",
        "San Francisco, CA - US",
        "U.S. Remote",
        "Remote - U.S.",
        "U.S.A.",
        "Global Remote",
        "Remote, Worldwide",
        "",
    ],
)
def test_us_locations(loc):
    assert feed.is_us(loc)


@pytest.mark.parametrize(
    "loc",
    [
        "Pune, MH, in",
        "Madrid, MD, es",
        "Toronto, ON, ca",
        "London",
        "Remote - Mexico",
        "Remote, Canada",
        "Remote in EMEA",
        "Dubai, UAE, ae",
        "Toronto, CA",
        "Tokyo",
        "Global",
        "Focus on usability",
    ],
)
def test_non_us_locations(loc):
    assert not feed.is_us(loc)


def test_malformed_rows_since_and_duplicates(feed_http):
    body = HEAD + _row("A", "SWE", "https://jobs.lever.co/a/1b2c3d4e-1111-2222-3333-444455556666", "2d")
    body += _row("Old", "SWE", "https://o.example/1", "60d")
    body += _row("A", "SWE", "https://jobs.lever.co/a/1b2c3d4e-1111-2222-3333-444455556666", "2d")  # same link twice
    body += _row("A", "SWE", "https://jobs.lever.co/a/1b2c3d4e-1111-2222-3333-444455556666?src=x", "2d")  # same id
    body += _row("C", "SWE", "javascript:alert(1)")
    body += _row("", "SWE", "https://d.example/1")
    body += "| **[E](https://resumax.ai/companies/e)** | SWE | NYC | [Apply](https://e.example/1) | 1d |\n"  # missing a cell
    body += "| **not a company** | SWE | NYC | [Apply](https://f.example/1) | x | 1d |\n"
    body += "## Not a category\n" + _row("G", "SWE", "https://g.example/1")
    feed_http[feed.URL] = body
    out = feed.fetch(FeedOptions(since=date.today() - timedelta(days=30)))
    assert [x.company for x in out] == ["A"] and out[0].ats == "lever"
    assert [str(c.url) for c in feed_http.calls] == [feed.URL]


def test_file_without_tables_is_an_error(feed_http):
    feed_http[feed.URL] = "# moved\nnothing here\n"
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())
