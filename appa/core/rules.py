"""Deterministic filtering. Every verdict carries human-readable reasons."""

from __future__ import annotations

import re

from appa.config import Candidate, Rules
from appa.core.models import Decision, Extraction, Job, Verdict
from appa.core.normalize import company as normalize_company


def on_manual_list(company: str | None, rules: Rules) -> bool:
    """True if the company is on your apply-myself list."""
    return normalize_company(company) in {normalize_company(c) for c in rules.manual_companies}


def title_rejects(title: str, rules: Rules) -> list[str]:
    """Cheap pre-filter on the title alone, run before spending an LLM call."""
    t = title.lower()

    def has(word: str) -> bool:
        """True if the title contains the word as a whole word, optionally with an -s or -ing ending."""
        # Whole word, allowing a plural or -ing ending ("engineer" matches "engineering",
        # "intern" does not match "internal").
        return re.search(rf"(?<!\w){re.escape(word.lower())}(?:s|ing)?(?!\w)", t) is not None

    hits = [f"title contains {w!r}" for w in rules.title_excludes if has(w)]
    if rules.title_includes and not any(has(w) for w in rules.title_includes):
        hits.append("title matches none of your target roles")
    return hits


# Deterministic cross-checks on the raw posting text. The model reads untrusted text, so
# a manipulated posting could talk it into "not mentioned"; these patterns can't be talked to.
_MENTIONS = {
    "sponsorship": re.compile(r"\bsponsor(ship|ing|ed)?\b|\bh-?1b\b|\bvisa\b", re.IGNORECASE),
    "citizenship": re.compile(
        r"\bu\.?s\.? citizen(s|ship)?\b|\bgreen card\b|\bpermanent resident|\bitar\b"
        r"|\bexport control|\bu\.?s\.? persons?\b",
        re.IGNORECASE,
    ),
    "clearance": re.compile(r"\b(security|secret|ts/sci|top secret)\s+clearance\b|\bts/sci\b", re.IGNORECASE),
}


def _squash(t: str) -> str:
    """Lowercase and collapse every run of non-letters/digits into one space, so a quote matches the posting
    regardless of punctuation, case and spacing."""
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _in_posting(quote: str, job: Job) -> bool:
    """True only if the model's quoted evidence actually appears in the posting text (the guard that stops
    hallucinated or injected quotes from rejecting a job)."""
    return bool(quote) and _squash(quote) in _squash(job.description)


def cross_check(job: Job, ex: Extraction) -> list[str]:
    """Reasons to send a job to a human when the model's "all clear" disagrees with the text."""
    issues = []
    if ex.sponsorship == "not_mentioned" and _MENTIONS["sponsorship"].search(job.description):
        issues.append("posting mentions sponsorship/visas but the extractor said not mentioned")
    if not ex.citizenship_required and not ex.citizenship_quote and _MENTIONS["citizenship"].search(job.description):
        issues.append("posting mentions citizenship/US-person/export control: confirm it isn't required")
    if not ex.clearance_required and not ex.clearance_quote and _MENTIONS["clearance"].search(job.description):
        issues.append("posting mentions a security clearance: confirm it isn't required")
    return issues


def decide(job: Job, ex: Extraction, rules: Rules, candidate: Candidate) -> Decision:
    """Apply the hard filters to an extraction: reject, apply-myself, review, or candidate, each with reasons."""
    reject: list[str] = []
    review: list[str] = cross_check(job, ex)

    def hard_no(reason: str, quote: str) -> None:
        """Reject with the quote if it is verbatim in the posting; otherwise send the job to review instead."""
        # Only reject on evidence found verbatim in the posting. A quote the model can't back
        # up (misread, or planted by the page) goes to a human: a wrong reject is a lost job.
        if _in_posting(quote, job):
            reject.append(f"{reason}: {quote!r}")
        else:
            review.append(f"{reason}, but the quote isn't in the posting, so check it: {quote[:120]!r}")

    # Work authorization
    if candidate.needs_sponsorship_future or candidate.needs_sponsorship_now:
        if ex.sponsorship == "no_sponsorship_now_or_future":
            hard_no("no sponsorship", ex.sponsorship_quote)
        elif ex.sponsorship == "no_sponsorship_unspecified":
            if candidate.needs_sponsorship_now:
                hard_no("no sponsorship", ex.sponsorship_quote)
            else:
                review.append(f"sponsorship unclear for OPT/future: {ex.sponsorship_quote!r}")
    if rules.reject_citizenship_required and ex.citizenship_required:
        hard_no("citizenship/US-person required", ex.citizenship_quote)
    if rules.reject_clearance_required and ex.clearance_required:
        hard_no("clearance required", ex.clearance_quote)

    # Experience
    if (
        ex.min_years_experience is not None
        and ex.min_years_experience > rules.max_years_experience
        and not ex.is_entry_level
    ):
        hard_no(f"{ex.min_years_experience}+ years required", ex.experience_quote)
    if ex.seniority == "senior" and not ex.is_entry_level:
        reject.append("senior-level role")
    reject += title_rejects(ex.title or job.title, rules)

    if reject:
        return Decision(verdict=Verdict.REJECT, reasons=reject)

    # Passed the filters. Now: who applies?
    if ex.suspicious_instructions:
        review.append("posting contains text aimed at AI tools: " + " | ".join(ex.suspicious_instructions)[:300])
    if ex.is_staffing_agency:
        review.append("posted by a staffing agency")
    if on_manual_list(ex.company or job.company, rules):
        return Decision(verdict=Verdict.MANUAL, reasons=["on your apply-myself list", *review])
    if review:
        return Decision(verdict=Verdict.REVIEW, reasons=review)
    reasons = ["passes all filters"]
    if ex.has_essay_questions:
        reasons.append("has essay/cover-letter questions: will need your input")
    return Decision(verdict=Verdict.CANDIDATE, reasons=reasons)


# ---- screening without a model ---------------------------------------------------------------

_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_YEARS = re.compile(
    _NUM + r"\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?)\b(?:\s+of)?[^.\n]{0,60}?"
    r"\b(?:experience|working|as an?|in (?:a|the) role)"
    r"|experience\s*:\s*" + _NUM + r"\s*\+?\s*(?:years?|yrs?)\b"
    r"|at least\s+" + _NUM + r"\s+(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NEW_GRAD = re.compile(
    r"\bnew[- ]?grad|\bentry[- ]level\b|\bearly[- ]career\b|\buniversity grad"
    r"|\brecent grad(?:uate)?s?\b|\bcollege grad",
    re.IGNORECASE,
)


_SPONSOR_OK = re.compile(
    r"\b(?:will|can|happy to|able to)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor"
    r"|\bsponsorship\s+(?:is\s+)?(?:available|offered|provided)\b|\bvisa sponsorship\s*:\s*yes",
    re.IGNORECASE,
)
_SPONSOR_NO = re.compile(
    r"\b(?:not|unable|cannot|can't|won't|no|never)\b[^.\n]{0,50}\bsponsor"
    r"|\bwithout\b[^.\n]{0,50}\bsponsor|\bsponsor\w*\b[^.\n]{0,40}\b(?:not|unavailable)\b"
    r"|\bauthori[sz]ed to work\b[^.\n]{0,80}\bwithout\b"
    r"|\bsponsor\w*\b[^.\n]{0,40}[:?]\s*no\b",
    re.IGNORECASE,
)
# Work-authorization limits that never say "sponsor" (they matter for F-1/OPT the same way).
_AUTH_LIMITS = re.compile(
    r"\b(?:OPT|CPT)\b|\bunrestricted\s+(?:work\s+)?authori[sz]ation|\bpermanent basis\b"
    r"|\bwithout\s+(?:the\s+)?need\s+for\b[^.\n]{0,30}\bvisa",
    re.IGNORECASE,
)


def _snippet(text: str, m: re.Match, width: int = 90) -> str:
    """The text around a match, on one line, for a reason you can read."""
    a, b = max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)
    return " ".join(text[a:b].split())


def keyword_screen(job: Job, rules: Rules) -> Decision:
    """Screen without a model: anything that might matter (sponsorship, citizenship, clearance, years of
    experience, hidden text) sends the job to review with the passage quoted. Never rejects: a reject
    needs a model's verbatim evidence. A clean posting is a candidate, labelled as keyword-screened."""
    text = job.description or ""
    if len(text.strip()) < 200:
        return Decision(verdict=Verdict.REVIEW, reasons=["keyword screen: no posting text to check; look at the link"])
    flags = []
    for what, pat in _MENTIONS.items():
        if what == "sponsorship" and _SPONSOR_OK.search(text) and not _SPONSOR_NO.search(text):
            continue  # only says it sponsors
        if m := (_SPONSOR_NO.search(text) if what == "sponsorship" else None) or pat.search(text):
            flags.append(f"mentions {what}: \"{_snippet(text, m)}\"")
    if m := _AUTH_LIMITS.search(text):
        flags.append(f"mentions work authorization limits: \"{_snippet(text, m)}\"")
    early = bool(_NEW_GRAD.search(job.title))
    for m in _YEARS.finditer(text):
        word = next(g for g in m.groups() if g).lower()
        n = int(word) if word.isdigit() else _WORDS[word]
        if n > rules.max_years_experience and not early:
            flags.append(f"asks for {n}+ years: \"{_snippet(text, m)}\"")
            break
    if "[HIDDEN-ON-PAGE]" in text:
        flags.append("the page hides text from readers (possible instructions aimed at AI tools)")
    if flags:
        return Decision(verdict=Verdict.REVIEW, reasons=["keyword screen (no model key): check this", *flags[:4]])
    return Decision(
        verdict=Verdict.CANDIDATE,
        reasons=[
            "keyword screen only (no model key): no sponsorship, citizenship, clearance, "
            "experience or hidden-text flags found"
        ],
    )
