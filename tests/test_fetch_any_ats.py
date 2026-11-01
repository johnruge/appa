"""Single links on any ATS with a board adapter go through that adapter (no network)."""

from __future__ import annotations

import pytest

from appa import feeds
from appa.core.models import Job
from appa.feeds import BoardFeed, Listing
from appa.sources import fetch

TEXT = "Build data pipelines in Python. " * 20


@pytest.fixture()
def fake_ats(monkeypatch):
    """A fake ATS 'acmeats' whose board lists two postings, one without text (describe() supplies it)."""
    calls = {"board": 0, "describe": 0, "page": 0}

    def board_fetch(slug, opts=None):
        calls["board"] += 1
        return [
            Listing(
                company="Acme",
                title="Software Engineer, New Grad",
                url="https://acme.acmeats.example/jobs/101",
                ats="acmeats",
                ats_job_id="101",
                description=TEXT,
            ),
            Listing(
                company="Acme",
                title="Data Engineer",
                url="https://acme.acmeats.example/jobs/202",
                ats="acmeats",
                ats_job_id="R-202",
            ),
        ]

    def describe(x):
        calls["describe"] += 1
        return TEXT

    def board_of(url):
        return "acme" if url.startswith("https://acme.acmeats.example/") else None

    def page(url):
        calls["page"] += 1
        return Job(company="page", title="page", url=url, description="from the page")

    monkeypatch.setattr(feeds, "_loaded", True)
    monkeypatch.setattr(
        feeds, "BOARDS", {"acmeats": BoardFeed(ats="acmeats", fetch=board_fetch, board_of=board_of, describe=describe)}
    )
    monkeypatch.setattr(fetch.generic, "fetch", page)
    monkeypatch.setattr(fetch, "_boards", {})
    return calls


def test_link_is_read_through_its_ats_adapter(fake_ats):
    job = fetch.fetch_posting("https://acme.acmeats.example/jobs/101/?utm_source=x")
    assert (job.company, job.title, job.description) == ("Acme", "Software Engineer, New Grad", TEXT)
    assert job.url == "https://acme.acmeats.example/jobs/101/?utm_source=x" and fake_ats["page"] == 0


def test_missing_text_comes_from_describe_and_boards_are_cached(fake_ats):
    job = fetch.fetch_posting("https://acme.acmeats.example/apply/R-202")  # matched by the posting ID
    assert job.title == "Data Engineer" and job.description == TEXT and fake_ats["describe"] == 1
    fetch.fetch_posting("https://acme.acmeats.example/jobs/101")
    assert fake_ats["board"] == 1  # one board fetch for both links


def test_postings_not_on_the_board_fall_back_to_the_page(fake_ats):
    assert fetch.fetch_posting("https://acme.acmeats.example/jobs/999").description == "from the page"
    assert fetch.fetch_posting("https://other.example/careers/1").description == "from the page"
