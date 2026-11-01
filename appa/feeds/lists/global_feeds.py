"""Single-feed job boards that are written as board adapters (in appa/feeds/boards) but have one fixed feed,
not one per company. They run here as list sources, so every sweep reads them; they're removed from the
board rotation, where no company link would ever point at them."""

from __future__ import annotations

import importlib

from appa.feeds import BOARDS, LOAD_ERRORS, FeedOptions, ListFeed, register_list

# board module -> (list name, short description, API key env var or None, included in "all")
GLOBAL = {
    "authenticjobs_rss": ("authenticjobs", "Authentic Jobs RSS: US developer and design roles", None, True),
    "cryptojobslist_rss": ("cryptojobslist", "CryptoJobsList RSS: web3, crypto and fintech roles", None, True),
    "jobspresso_rss": ("jobspresso", "Jobspresso RSS: curated remote roles", None, True),
    "larajobs_rss": ("larajobs", "LaraJobs RSS: PHP/Laravel roles", None, True),
    "nodesk_rss": ("nodesk", "NoDesk RSS: curated remote roles", None, True),
    "python_jobs_rss": ("python_jobs", "Python.org job board RSS", None, True),
    "remotefirstjobs_rss": ("remotefirstjobs", "RemoteFirstJobs RSS: high-volume remote roles", None, True),
    "rubyonrails_jobs_rss": ("rubyonrails_jobs", "Ruby on Rails job board RSS", None, True),
    "vuejobs_rss": ("vuejobs", "VueJobs RSS: Vue/Nuxt frontend and full-stack roles", None, True),
    "jsearch_rapidapi": (
        "jsearch",
        "JSearch (RapidAPI): Google for Jobs results; free plan 200 requests/month",
        "RAPIDAPI_KEY",
        False,
    ),
}


def _as_list(module: str, name: str, description: str, key_env: str | None, default: bool) -> None:
    """Import the board module, take its BoardFeed out of the rotation and register it as a list source."""
    board = importlib.import_module(f"appa.feeds.boards.{module}").BOARD
    BOARDS.pop(board.ats, None)

    def fetch(opts: FeedOptions, b=board):
        """Read the board's one fixed feed."""
        return b.fetch(b.example, opts)

    register_list(
        ListFeed(
            name=name,
            description=description,
            fetch=fetch,
            describe=board.describe,
            key_env=key_env,
            terms=board.terms,
            default=default,
        )
    )


for _module, (_name, _desc, _key, _default) in GLOBAL.items():
    try:
        _as_list(_module, _name, _desc, _key, _default)
    except Exception as e:  # one broken feed must not take the others down
        LOAD_ERRORS[f"appa.feeds.boards.{_module}"] = f"{type(e).__name__}: {e}"[:300]
