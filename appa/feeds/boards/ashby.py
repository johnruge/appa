"""Ashby job boards, through the public API appa already uses for Ashby links (appa/sources/ats.py)."""

from __future__ import annotations

from appa.core.ats_urls import parse_url
from appa.feeds import BoardFeed, FeedOptions, Listing, register_board
from appa.sources import ats


def fetch(slug: str, opts: FeedOptions | None = None) -> list[Listing]:
    """Every open posting on this company's Ashby board, with descriptions."""
    return [
        Listing(
            company=j.company,
            title=j.title,
            url=j.url,
            location=j.location,
            description=j.description,
            ats=j.ats,
            ats_job_id=j.ats_job_id,
            source_ref=j.ats_job_id,
        )
        for j in ats.fetch_board("ashby", slug)
        if j.url.startswith(("http://", "https://"))
    ]


def board_of(url: str) -> str | None:
    """The Ashby board slug in a link, if it is a Ashby link."""
    ref = parse_url(url)
    return ref.slug if ref and ref.ats == "ashby" and ref.slug else None


BOARD = register_board(
    BoardFeed(ats="ashby", fetch=fetch, board_of=board_of, example="ramp", hosts=("api.ashbyhq.com",))
)
