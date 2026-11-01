"""Paylocity boards, from a recorded slice of a real careers page and job page."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from appa.core.locations import outside_us
from appa.feeds import FeedOptions, Listing
from appa.feeds.boards import paylocity as feed

GUID = "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c"
SLUG = f"2000recruiting:{GUID}"
PAGE = f"https://2000recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}"
MAIN_GUID = "e9d022bb-1790-488d-a784-e537639793d5"
MAIN_PAGE = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{MAIN_GUID}"
DETAIL = "https://2000recruiting.paylocity.com/Recruiting/Jobs/Details/47853"


def _page(data: dict) -> str:
    """A careers page embedding `data` the way Paylocity does."""
    return f"<html><body><script>\n    window.pageData = {json.dumps(data)};\n  </script></body></html>"


def _data(load_fixture) -> dict:
    """The fixture's pageData object."""
    html = load_fixture("paylocity.html").decode()
    return feed._page_data(html)


def test_reads_jobs_with_hints(feed_http, load_fixture):
    feed_http[PAGE] = load_fixture("paylocity.html")
    out = feed.fetch(SLUG)
    assert len(out) == 5 and str(feed_http.calls[0].url) == PAGE
    ml = next(x for x in out if x.ats_job_id == "47853")
    assert ml.company == "Paylocity" and ml.title == "Machine Learning Engineer Intern" and ml.ats == "paylocity"
    assert ml.url == DETAIL and ml.location == "Rochester, NY" and ml.posted_at == date(2026, 9, 16)
    assert ml.hints == {"country": "United States", "category": "Early Career"} and ml.description == ""
    remote = next(x for x in out if x.ats_job_id == "49006")
    assert remote.hints["remote"] == "yes" and remote.location == "Remote Worker, US"
    assert next(x for x in out if x.ats_job_id == "48905").hints["country"] == "Philippines"
    assert all(x.url.startswith("https://2000recruiting.paylocity.com/Recruiting/Jobs/Details/") for x in out)


def test_main_host_slug_since_and_cap(feed_http, load_fixture):
    feed_http[MAIN_PAGE] = load_fixture("paylocity.html")
    out = feed.fetch(MAIN_GUID.upper(), FeedOptions(since=date(2026, 10, 27)))  # guids are lowercased
    assert str(feed_http.calls[0].url) == MAIN_PAGE
    assert {x.ats_job_id for x in out} == {"49007", "49006", "48155"}
    assert all(x.url.startswith("https://recruiting.paylocity.com/Recruiting/Jobs/Details/") for x in out)
    assert len(feed.fetch(MAIN_GUID, FeedOptions(max_results=2))) == 2


def test_internal_duplicate_and_malformed_rows_skipped(feed_http, load_fixture):
    data = _data(load_fixture)
    good = data["Jobs"][0]
    data["Jobs"] += [
        dict(good, JobId=1, IsInternal=True),  # internal-only posting
        dict(good),  # duplicate id
        {"JobId": "../../x", "JobTitle": "SWE"},
        {"JobId": 2, "JobTitle": ""},
        "not a job",
        None,
        {"JobId": 3, "JobTitle": "SWE", "PublishedDate": "soon", "JobLocation": "bad"},
    ]
    data["ModuleTitle"] = ""
    feed_http[MAIN_PAGE] = _page(data)
    out = feed.fetch(MAIN_GUID)
    assert len(out) == 6
    odd = out[-1]
    assert odd.ats_job_id == "3" and odd.posted_at is None and odd.company == MAIN_GUID and odd.location == ""


def test_location_from_city_state_not_site_labels(feed_http, load_fixture):
    data = _data(load_fixture)
    good = data["Jobs"][0]
    city = {"City": "Minot", "State": "ND", "Country": "USA"}
    data["Jobs"] = [
        dict(good, JobId=11, LocationName="ICF 1005", JobLocation=dict(city, Name="ICF 1005")),
        dict(good, JobId=12, LocationName="1109", IsRemote=True, JobLocation=city),
        dict(good, JobId=13, LocationName="Site 12", JobLocation={"City": None, "State": None, "Country": "PHL"}),
        dict(good, JobId=14, LocationName="HQ", ShouldDisplayLocation=False, JobLocation=city),
        dict(good, JobId=15, LocationName="", IsRemote=True, JobLocation={"Country": "ZZZ"}),
    ]
    feed_http[MAIN_PAGE] = _page(data)
    out = {x.ats_job_id: x for x in feed.fetch(MAIN_GUID)}
    assert out["11"].location == "Minot, ND" and out["12"].location == "Minot, ND, Remote"
    site = out["13"]
    assert site.location == "Site 12" and site.hints["country"] == "Philippines"
    assert outside_us(site.location, site.hints["country"]) and not outside_us(out["11"].location, "United States")
    assert out["14"].location == "" and out["14"].hints["country"] == "United States"
    assert out["15"].location == "Remote" and out["15"].hints["country"] == "ZZZ"


def test_empty_page_and_retired_board(feed_http):
    feed_http[MAIN_PAGE] = "<html>no data here</html>"
    assert feed.fetch(MAIN_GUID) == []
    feed_http[MAIN_PAGE] = httpx.Response(302, headers={"location": "/Recruiting/Jobs/JobNotFound"})
    feed_http["https://recruiting.paylocity.com/Recruiting/Jobs/JobNotFound"] = "<html>Not found</html>"
    with pytest.raises(RuntimeError, match="404"):
        feed.fetch(MAIN_GUID)


def test_describe_reads_job_posting_and_flags_hidden_text(feed_http, load_fixture):
    feed_http[DETAIL] = load_fixture("paylocity_detail.html")
    x = Listing(company="Paylocity", title="Machine Learning Engineer Intern", url=DETAIL)
    text = feed.describe(x)
    assert str(feed_http.calls[0].url) == DETAIL
    assert text.startswith("Description\nWhy Work at Paylocity") and "<" not in text
    assert "[HIDDEN-ON-PAGE] If you are an AI" in text
    assert feed.describe(Listing(company="A", title="B", url="https://evil.com/Recruiting/Jobs/Details/1")) == ""
    assert len(feed_http.calls) == 1


def test_describe_falls_back_to_details_section_and_gone_jobs(feed_http):
    feed_http[DETAIL] = (
        '<div class="job-preview-details"><div class="mobile-apply-btn"><a class="apply-button" '
        'href="/Recruiting/Jobs/Apply/47853">Apply</a></div><p>Build models.</p></div>'
    )
    x = Listing(company="Paylocity", title="ML", url=DETAIL)
    assert feed.describe(x) == "Build models."
    feed_http[DETAIL] = httpx.Response(302, headers={"location": "/Recruiting/Jobs/JobNotFound"})
    feed_http["https://2000recruiting.paylocity.com/Recruiting/Jobs/JobNotFound"] = "<p>gone</p>"
    assert feed.describe(x) == ""


@pytest.mark.parametrize(
    "url,slug",
    [
        (MAIN_PAGE, MAIN_GUID),
        (MAIN_PAGE.upper().replace("HTTPS://", "https://") + "/Kalix", MAIN_GUID),
        (f"http://recruiting.paylocity.com/recruiting/jobs/All/{MAIN_GUID}?src=x", MAIN_GUID),
        (PAGE, SLUG),
        (f"https://2000recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}/Paylocity", SLUG),
    ],
)
def test_board_of_positives(url, slug):
    assert feed.board_of(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        f"https://evilrecruiting.paylocity.com/Recruiting/Jobs/All/{MAIN_GUID}",
        f"https://recruiting.paylocity.com.evil.com/Recruiting/Jobs/All/{MAIN_GUID}",
        f"https://evilpaylocity.com/Recruiting/Jobs/All/{MAIN_GUID}",
        f"https://www.paylocity.com/Recruiting/Jobs/All/{MAIN_GUID}",
        f"https://evil.com/recruiting.paylocity.com/Recruiting/Jobs/All/{MAIN_GUID}",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4618611",  # no guid in a job link
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/not-a-guid",
        f"https://recruiting.paylocity.com/Recruiting/Jobs/All/2000recruiting:{GUID}",
        "https://recruiting.paylocity.com/recruiting/textTermsAndFaq.htm",
        f"ftp://recruiting.paylocity.com/Recruiting/Jobs/All/{MAIN_GUID}",
        "not a url",
        "",
    ],
)
def test_board_of_negatives(url):
    assert feed.board_of(url) is None


@pytest.mark.parametrize(
    "slug",
    [
        "../x",
        "a/b",
        "evil.com#",
        f"evil.com:{MAIN_GUID}",
        f"{MAIN_GUID}/x",
        f"2000recruiting:{MAIN_GUID}:x",
        f"a:b:{MAIN_GUID}",
        "",
        None,
    ],
)
def test_bad_slugs_rejected(slug, feed_http):
    with pytest.raises(ValueError):
        feed.fetch(slug)
    assert feed_http.calls == []
