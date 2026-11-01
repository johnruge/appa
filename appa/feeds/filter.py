"""The free filter every listing passes before any page fetch or model call: title rules, your
never-apply list, the source's own labels (citizenship, internship, seniority), age, and location.
A filtered listing stays in the inbox with its reason, so you can see and undo it (`appa listings`)."""

from __future__ import annotations

import re
from datetime import date

from appa.config import Config
from appa.core import rules
from appa.core.locations import outside_us
from appa.core.normalize import company as norm_company
from appa.feeds import Listing

# Level III and up (or "Sr" without a dot) is never a new-grad role; level II stays in (often 0-2 years).
_SENIOR_LEVEL = re.compile(
    r"\b(?:engineer|developer|swe|sde|scientist|analyst)\s*,?\s*(?:III|IV|V|3|4|5)\b"
    r"|\b(?:III|IV)\b|\bsr\b",
    re.IGNORECASE,
)


def reason_to_skip(x: Listing, cfg: Config, today: date) -> str | None:
    """Why this listing isn't worth screening, or None if it is."""
    if norm_company(x.company) in {norm_company(c) for c in cfg.rules.never_apply}:
        return "on your never-apply list"
    if hits := rules.title_rejects(x.title, cfg.rules):
        return hits[0]
    if m := _SENIOR_LEVEL.search(x.title):
        return f"title says level {m.group(0).strip()} (above new grad)"
    h = x.hints
    if h.get("citizenship_required") == "yes" and cfg.rules.reject_citizenship_required:
        return f"the source says U.S. citizenship is required ({h.get('sponsorship') or 'citizenship'})"
    if h.get("level") in ("senior", "intern") or h.get("employment_type") in ("internship", "part_time"):
        return f"the source lists it as {h.get('level') or h.get('employment_type')}"
    if x.posted_at and cfg.feeds.max_age_days and (today - x.posted_at).days > cfg.feeds.max_age_days:
        return f"posted {(today - x.posted_at).days} days ago (limit {cfg.feeds.max_age_days})"
    if cfg.feeds.us_only and outside_us(x.location, h.get("country")):
        return f"outside the US: {x.location or h.get('country')}"[:200]
    return None
