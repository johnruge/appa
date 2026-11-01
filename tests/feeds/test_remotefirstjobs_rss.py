"""RemoteFirstJobs RSS, from a recorded slice of the real site-wide feed."""

from __future__ import annotations

from datetime import date

import pytest

from appa.feeds import FeedOptions
from appa.feeds.boards import remotefirstjobs_rss as feed

URL = "https://remotefirstjobs.com/rss/jobs.rss"
FIX = "remotefirstjobs_rss.rss"


def _item(
    jid: int,
    title: str = "Software Engineer at Acme",
    pub: str = "Fri, 30 Oct 2026 10:00:00 +0000",
    link: str | None = None,
    desc: str = "&lt;p&gt;Build things&lt;/p&gt;",
) -> str:
    """One RSS item in RemoteFirstJobs' shape."""
    link = link if link is not None else f"https://remotefirstjobs.com/companies/acme/jobs/software-engineer-{jid}"
    return (
        f"<item><title>{title}</title><link>{link}</link><description>{desc}</description>"
        f'<guid isPermaLink="true">software-engineer-{jid}</guid><pubDate>{pub}</pubDate></item>'
    )


def _rss(*items: str) -> str:
    """An RSS document around the given items."""
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>{"".join(items)}</channel></rss>'


def test_parses_fixture(feed_http, load_fixture):
    feed_http[URL] = load_fixture(FIX)
    out = feed.fetch("all")
    assert len(feed_http.calls) == 1 and str(feed_http.calls[0].url) == URL
    assert [x.company for x in out] == ["Urban Grid", "Fivetran", "Suvoda", "Huntress", "Litify", "GuidePoint Security"]
    assert out.pop(1).title == "Senior Sales Engineer, Enterprise"  # kept: appa's own filters decide
    de = out[0]
    assert de.title == "Performance Data Engineer"
    assert de.url == "https://remotefirstjobs.com/companies/urban-grid/jobs/performance-data-engineer-877817"
    assert (de.ats_job_id, de.source_ref) == ("877817", "performance-data-engineer-877817")
    assert de.posted_at == date(2026, 10, 30) and de.location == "Remote" and de.ats == "unknown"
    assert de.hints["sponsorship"].startswith("Visa sponsorship or transfer visas, including H-1B")
    assert "Panthera Grid Partners" in de.description and "<p" not in de.description and "&lt;" not in de.description
    assert (
        out[1].hints["level"] == "entry"
        and out[1].title == "Junior Technical (Support) Analyst w. Python (second shift)"
    )
    assert out[2].hints == {"remote": "yes", "salary": "$215,000 to $245,000", "level": "senior"}
    assert out[3].hints["salary"] == "$115-120k" and "level" not in out[3].hints
    gp = out[4]
    assert gp.hints["country"] == "United States" and gp.location == "Remote (United States)"
    assert "unable to sponsor" in gp.hints["sponsorship"]
    assert all(x.url.startswith("https://remotefirstjobs.com/companies/") and x.ats_job_id.isdigit() for x in out)


def test_feed_urls_and_single_request(feed_http):
    feed_http["https://remotefirstjobs.com/rss/jobs/software-development.rss"] = _rss(*[_item(i) for i in range(100)])
    assert len(feed.fetch("Software-Development")) == 100 and len(feed_http.calls) == 1  # no paging
    assert len(feed.fetch("software-development", FeedOptions(max_results=5))) == 5


def test_since_dedupe_hints_and_malformed_rows(feed_http):
    feed_http[URL] = _rss(
        _item(
            1,
            desc="&lt;p&gt;US only. Must be a U.S. citizen. &lt;a href='https://boards.greenhouse.io/acme/jobs/9'&gt;x&lt;/a&gt;&lt;/p&gt;",
        ),
        _item(2, pub="Mon, 05 Jan 2026 10:00:00 +0000"),
        _item(3, link="javascript:alert(1)"),
        _item(4, link="https://evil.example/companies/acme/jobs/software-engineer-4"),
        _item(5, title="Software Engineer"),  # no " at Company"
        _item(6, title=" at Acme"),  # empty title
        _item(1),  # duplicate ID
        _item(7, pub="not a date", title="Hybrid Software Engineering Intern at Acme at Night"),
        _item(8, title="Account Executive at Acme"),  # not a software role
    )
    out = feed.fetch("all", FeedOptions(since=date(2026, 6, 1)))
    assert [x.ats_job_id for x in out] == ["1", "7"]
    one = out[0]
    assert one.hints["country"] == "United States" and one.hints["citizenship_required"] == "yes"
    assert one.hints["links"] == "https://boards.greenhouse.io/acme/jobs/9"
    seven = out[1]
    assert (seven.company, seven.title, seven.posted_at) == (
        "Night",
        "Hybrid Software Engineering Intern at Acme",
        None,
    )
    assert (
        seven.hints["remote"] == "hybrid"
        and seven.hints["level"] == "intern"
        and seven.hints["employment_type"] == "internship"
    )


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://remotefirstjobs.com/companies/fivetran/jobs/senior-sales-engineer-enterprise-877816", "all"),
        ("https://www.remotefirstjobs.com/companies/acme/jobs/swe-1?ref=x", "all"),
        ("https://remotefirstjobs.com/rss/jobs.rss", "all"),
        ("https://remotefirstjobs.com/jobs", "all"),
        ("https://remotefirstjobs.com/jobs/python", "python"),
        ("https://remotefirstjobs.com/rss/jobs/entry-level.rss", "entry-level"),
        ("https://REMOTEFIRSTJOBS.com/jobs/Software-Development/", "software-development"),
    ],
)
def test_board_of_positive(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://evilremotefirstjobs.com/jobs/python",
        "https://remotefirstjobs.com.evil.com/jobs/python",
        "https://cdn.remotefirstjobs.com/jobs/python",
        "https://example.com/?u=https://remotefirstjobs.com/jobs/python",
        "https://remotefirstjobs.com/",
        "https://remotefirstjobs.com/companies/fivetran",
        "https://remotefirstjobs.com/blog/some-post",
        "https://remotefirstjobs.com/jobs/a/b",
        "https://remotefirstjobs.com/jobs/-bad",
        "https://boards.greenhouse.io/acme/jobs/1",
        "not a url",
    ],
)
def test_board_of_negative(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize("slug", ["../x", "a/b", "evil.com#", "", "-x", "x-", "a b", "a" * 81, None])
def test_slug_validation(slug):
    with pytest.raises(ValueError):
        feed.feed_url(slug)


def test_feed_url():
    assert feed.feed_url("all") == URL
    assert feed.feed_url("python") == "https://remotefirstjobs.com/rss/jobs/python.rss"


@pytest.mark.parametrize(
    "title,text,country",
    [
        (
            "Sr. Analytics Engineer (Brazil and Argentina Only)",
            "Remote. We will not be considering US based candidates at this time.",
            "",
        ),
        ("Data Engineer", "We will not be considering US based candidates at this time.", ""),
        ("Data Engineer (LatAm Only)", "Remote (US)", ""),
        ("Data Engineer (US Only)", "", "United States"),
        ("Radar Perception Engineer", "Work Location: Remote in the United States", "United States"),
        ("Engineer", "Remote within US.", "United States"),
        ("Engineer", "No relocation needed. US based candidates only.", "United States"),
    ],
)
def test_country_hint(title, text, country):
    assert feed._hints(title, text, "")["country"] == country


def test_sponsorship_hint_joins_colon_line():
    hints = feed._hints("Radar Perception Engineer", "Visa sponsorship\n: Available for this position.", "")
    assert hints["sponsorship"] == "Visa sponsorship: Available for this position."


@pytest.mark.parametrize(
    "title,level",
    [
        ("Software Engineer I", "entry"),
        ("Data Engineer 1", "entry"),
        ("Software Engineer II", ""),
        ("Software Engineer in Test", ""),
        ("Senior Software Engineer", "senior"),
        ("Senior Software Engineer I", "senior"),
    ],
)
def test_numeric_levels(title, level):
    assert feed._level(title) == level


@pytest.mark.parametrize(
    "title,keep",
    [
        ("Associate Security Researcher", True),
        ("IT Support Analyst", True),
        ("Sales Engineer", True),
        ("Marketing Engineer I", True),
        ("Account Executive", False),
        ("Marketing Manager", False),
    ],
)
def test_is_swe_is_loose(title, keep):
    assert feed.is_swe(title) is keep
