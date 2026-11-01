"""QuantScraper quant graduate roles, from a recorded slice of the real README."""

from __future__ import annotations

from datetime import date

import pytest

from appa.core.locations import outside_us
from appa.feeds import FeedOptions
from appa.feeds.lists import quantscraper_quant_newgrad as feed

TODAY = date(2026, 10, 30)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(feed, "_today", lambda: TODAY)


def test_reads_only_graduate_rows(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("quantscraper_quant_newgrad.md")
    out = feed.fetch(FeedOptions())
    assert len(out) == 7  # internships and programmes are skipped
    assert not any("Intern" in x.title or "Register" in x.title for x in out)
    assert out == sorted(out, key=lambda x: x.posted_at or date.min, reverse=True)
    five = next(x for x in out if x.company == "Five Rings")
    assert five.url == "https://job-boards.greenhouse.io/fiveringsllc/jobs/5344213008"
    assert five.ats == "greenhouse" and five.ats_job_id == "5344213008"
    assert five.location == "Amsterdam" and "salary" not in five.hints
    assert five.hints["category"] == "Trading / Quant" and five.hints["level"] == "new_grad"
    jump = next(x for x in out if x.company == "Jump Trading")
    assert jump.hints["category"] == "Quantitative research / Quant"  # focus from the <summary>
    assert jump.hints["salary"] == "USD 200k–250k" and jump.ats_job_id == "8209424"
    sw = next(x for x in out if "C++" in x.title)
    assert sw.company == "Tower Research Capital" and sw.hints["category"] == "Software & technology / Software"
    assert {x.company for x in out} >= {"Chicago Trading Company", "Bridgewater Associates", "Marshall Wace"}


def test_dates_continuations_and_malformed_rows():
    text = "\n".join(
        [
            "## Internships",
            "| Company | Role | Location | Focus | Salary | Posted |",
            "|---|---|---|---|---|---|",
            "| **A** | [Intern](https://a.example/intern) | NYC | Quant | — | Oct 1 |",
            "",
            "## Graduate roles",
            "",
            "### Trading — graduate roles",
            "| Company | Role | Location | Focus | Salary | Posted |",
            "|---|---|---|---|---|---|",
            "| **[Acme](https://quant-scraper.com/companies/acme)** | [Trader](https://acme.example/1) | Remote | Quant | — | Oct 29 |",
            "| ↳ | [Trader II](https://acme.example/2) | Chicago, NYC +2 | General | — | Dec 5 |",  # Dec 5 2025
            "| **Beta** | [Old](https://beta.example/1) | — | General | — | Jun 24, 2025 |",
            "| **Gamma** | [No date](https://gamma.example/1) | London | General | — | — |",
            "| **Bad** | not a link | London | General | — | Oct 1 |",
            "| **Bad2** | [JS](javascript:alert(1)) | London | General | — | Oct 1 |",
            "| **Short** | [x](https://short.example/1) |",
            "| **Dup** | [Trader](https://acme.example/1) | NYC | Quant | — | Oct 29 |",
            "",
            "## Programmes & insight weeks",
            "| Company | Role | Location | Focus | Salary | Posted |",
            "|---|---|---|---|---|---|",
            "| **P** | [Week](https://p.example/1) | NYC | General | — | Oct 1 |",
        ]
    )
    out = feed._parse(text.replace("\n## Graduate roles\n", "\r\n## Graduate roles \r\n"), TODAY)
    assert [x.url for x in out] == [
        "https://acme.example/1",
        "https://acme.example/2",
        "https://beta.example/1",
        "https://gamma.example/1",
    ]
    a1, a2, beta, gamma = out
    assert a1.hints["remote"] == "yes" and a1.posted_at == date(2026, 10, 29)
    assert a2.company == "Acme" and a2.posted_at == date(2025, 12, 5) and a2.location == "Chicago, NYC"
    assert beta.posted_at == date(2025, 6, 24) and beta.location == ""
    assert gamma.posted_at is None


def test_since_and_cap(feed_http, load_fixture):
    feed_http[feed.URL] = load_fixture("quantscraper_quant_newgrad.md")
    out = feed.fetch(FeedOptions(since=date(2026, 10, 29), max_results=100))
    assert all(x.posted_at is None or x.posted_at >= date(2026, 10, 29) for x in out) and len(out) == 5
    assert len(feed.fetch(FeedOptions(max_results=2))) == 2


def test_missing_section_raises(feed_http):
    feed_http[feed.URL] = "# Something else\n\n## Internships\n"
    with pytest.raises(ValueError):
        feed.fetch(FeedOptions())


def test_hidden_offices_keep_abroad_rows_for_us_filter():
    text = "\n".join(
        [
            "## Graduate roles",
            "### Trading",
            "| Company | Role | Location | Focus | Salary | Posted |",
            "|---|---|---|---|---|---|",
            "| **Flow Traders** | [PhD QR](https://job-boards.greenhouse.io/flowtraders/jobs/8156203) | Amsterdam, Hong Kong +1 | Quant | — | Oct 29 |",
            "| **Sq** | [SWE](https://sq.example/1) | London, New York +3 | Software | — | Oct 29 |",
            "| **Ab** | [Trader](https://ab.example/1) | Amsterdam | Quant | — | Oct 29 |",
        ]
    )
    flow, sq, ab = feed._parse(text, TODAY)
    assert flow.location == "" and not outside_us(flow.location)  # a hidden office may be New York
    assert flow.ats == "greenhouse" and flow.ats_job_id == "8156203"
    assert sq.location == "London, New York" and not outside_us(sq.location)
    assert ab.location == "Amsterdam" and outside_us(ab.location)


def test_mon_dd_allows_utc_slack_and_leap_days():
    assert feed._posted("Oct 31", date(2026, 10, 30)) == date(2026, 10, 31)  # README is a UTC day ahead
    assert feed._posted("Nov 1", date(2026, 10, 30)) == date(2026, 11, 1)
    assert feed._posted("Nov 5", date(2026, 10, 30)) == date(2025, 11, 5)
    assert feed._posted("Feb 29", date(2027, 3, 1)) == date(2024, 2, 29)
    assert feed._posted("Feb 29", date(2028, 3, 1)) == date(2028, 2, 29)
    assert feed._posted("Feb 30", TODAY) is None and feed._posted("Foo 3", TODAY) is None
    assert feed._posted("Oct 31 extra", TODAY) is None


def test_regional_title_keeps_abroad_location():
    text = "\n".join(
        [
            "## Graduate roles",
            "### Research",
            "| Company | Role | Location | Focus | Salary | Posted |",
            "|---|---|---|---|---|---|",
            "| **Citadel** | [Quantitative Researcher - PhD Graduate (Europe)](https://cit.example/1) | London, Paris +2 | Quant | — | Oct 29 |",
        ]
    )
    (cit,) = feed._parse(text, TODAY)
    assert cit.location == "London, Paris" and outside_us(cit.location)
