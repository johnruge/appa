"""Find a company's real posting on its ATS, given only company + title.

Used for screenshots and for links that don't point straight at an ATS.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import httpx

from appa.core.models import Job
from appa.sources import ats

MATCH_THRESHOLD = 0.82


def slug_candidates(company: str) -> list[str]:
    """Likely ATS board slugs for a company name: joined, hyphenated, and first word."""
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|technologies|labs|hq)\b\.?", "", company.lower())
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    cands = ["".join(words), "-".join(words), words[0]]
    return list(dict.fromkeys(cands))


def _norm_title(t: str) -> str:
    """Lowercase a title, drop parentheticals and separators, and collapse spaces."""
    t = t.lower()
    t = re.sub(r"\(.*?\)|[,/|–-]", " ", t)
    return " ".join(t.split())


def title_score(a: str, b: str) -> float:
    """Similarity of two titles from 0 to 1; a truncated screenshot title matching the start scores 0.9."""
    a, b = _norm_title(a), _norm_title(b)
    if a == b:
        return 1.0
    if a.endswith("…") or a.endswith("..."):  # truncated titles from screenshots
        a = a.rstrip(".…").strip()
        if b.startswith(a):
            return 0.9
    return SequenceMatcher(None, a, b).ratio()


def find_boards(company: str) -> list[tuple[str, str, list[Job]]]:
    """Every non-empty ATS board for this company. Companies that switched ATS
    often leave a stale board behind, so we check all of them."""
    found = []
    for slug in slug_candidates(company):
        for name in ("greenhouse", "lever", "ashby"):
            try:
                jobs = ats.fetch_board(name, slug)
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
                continue
            if jobs:
                found.append((name, slug, jobs))
        if found:
            break  # don't try looser slugs once the exact one exists
    return found


def resolve(company: str, title: str, location: str = "") -> tuple[list[Job], str]:
    """Return (matches sorted best-first, note). One match = confident."""
    boards = find_boards(company)
    if not boards:
        return [], f"no Greenhouse/Lever/Ashby board found for {company!r}"
    scored = sorted(
        ((title_score(title, j.title), j) for _, _, jobs in boards for j in jobs),
        key=lambda x: -x[0],
    )
    where = ", ".join(f"{n}/{s}" for n, s, _ in boards)
    good = [j for s, j in scored if s >= MATCH_THRESHOLD]
    exact = [j for s, j in scored if s == 1.0]
    good = exact or good
    if location and len(good) > 1:
        loc = location.lower().split(",")[0].strip()
        narrowed = [j for j in good if loc and loc in j.location.lower()]
        good = narrowed or good
    for j in good:
        if j.company == j.company.lower():
            j.company = company  # slug -> the human-readable name
    if not good:
        best = scored[0] if scored else None
        hint = f" (closest: {best[1].title!r})" if best else ""
        return [], f"{company} uses {where} but no posting matched {title!r}{hint}"
    return good, where
