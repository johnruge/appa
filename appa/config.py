from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "profile.yaml"
ENV_PATH = ROOT / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines from .env into the environment. Real environment variables win,
    so production (where secrets come from the environment) never reads a stray file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Candidate(BaseModel):
    name: str
    application_email: str
    graduation: str = ""
    needs_sponsorship_now: bool = False
    needs_sponsorship_future: bool = True


class Rules(BaseModel):
    max_years_experience: int = 2
    reject_citizenship_required: bool = True
    reject_clearance_required: bool = True
    manual_companies: list[str] = []  # always queue for you (overrides profile.md)
    always_auto: list[str] = []  # never queue for you (overrides profile.md)
    never_apply: list[str] = []  # reject outright
    title_excludes: list[str] = []
    title_includes: list[str] = []
    # A new posting (different job ID) at a company you've applied to: allow it with a note,
    # flag it for review, or skip it. Rows with no link/job ID never count.
    same_company: Literal["allow", "flag", "skip"] = "allow"


class ApplyPolicy(BaseModel):
    auto_prepare: bool = False  # prefill new matches in your browser automatically (you still submit)


class FeedsPolicy(BaseModel):
    """Where postings come from (appa/feeds) and the free filters applied before any model call."""

    lists: list[str] = ["all"]  # list sources to read ("all" = every default one whose key is set)
    discover_boards: bool = True  # every link found adds its company board to the poll rotation
    boards_per_run: int = 150  # company boards polled per scout run (least recently polled first)
    queries: list[str] = [
        "software engineer new grad",
        "entry level software engineer",
        "junior software engineer",
        "software engineer 2027",
        "data engineer new grad",
        "machine learning engineer new grad",
        "associate software engineer",
    ]
    max_age_days: int = 45  # skip postings older than this (when the source gives a date)
    us_only: bool = True  # skip postings clearly outside the US (remote-anywhere is kept)
    screen_per_run: int = 150  # new postings screened per run (each may cost a model call)
    keyword_screen_without_key: bool = True  # no model key: screen by keywords (never rejects, flags for review)


class Companies(BaseModel):
    greenhouse: list[str] = []
    lever: list[str] = []
    ashby: list[str] = []


class Config(BaseModel):
    candidate: Candidate
    rules: Rules
    companies: Companies = Companies()
    apply: ApplyPolicy = ApplyPolicy()
    feeds: FeedsPolicy = FeedsPolicy()


def load(path: Path | None = None) -> Config:
    """Load and validate the YAML config; exits with a hint if the file is missing."""
    path = path or DEFAULT_PATH
    if not path.exists():
        raise SystemExit(
            f"No config at {path}. Copy config/profile.example.yaml to config/profile.yaml and fill it in."
        )
    return Config.model_validate(yaml.safe_load(path.read_text()))
