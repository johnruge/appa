"""Canonical forms used for dedupe. Raw values are always stored as entered."""

from __future__ import annotations

import re
from datetime import date, datetime

_SUFFIXES = (
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|technologies|technology|labs|hq|the)\b"
)


def company(name: str | None) -> str:
    """Company name for matching: lowercase, no parentheticals or legal suffixes, letters and digits only."""
    n = re.sub(r"\(.*?\)", "", (name or "").lower())
    n = re.sub(_SUFFIXES, "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def title(t: str | None) -> str:
    """Title for matching: lowercase, parentheticals and punctuation removed, spaces collapsed."""
    t = re.sub(r"\(.*?\)|[^a-z0-9 ]", " ", (t or "").lower())
    return " ".join(t.split())


def parse_date(s: str | None) -> date | None:
    """Parse a date in m/d/Y, Y-m-d or m/d/y form; None if blank, ValueError if unrecognized."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date {s!r}")
