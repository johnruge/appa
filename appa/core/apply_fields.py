"""Application form questions: what they ask, and how your answers map onto them.

Pure logic, no I/O. The runner reports each form as a list of FormQuestion; the planner
(appa/apply/planner.py) turns them into a fill plan using your answers bank.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal[
    "text",
    "email",
    "tel",
    "url",
    "number",
    "textarea",
    "select",
    "combobox",
    "autocomplete",
    "radio",
    "checkbox",
    "checkbox_group",
    "yesno",
    "file",
]
CHOICE_KINDS = {"select", "combobox", "radio", "checkbox_group", "yesno"}


class FormQuestion(BaseModel):
    ref: str = Field(max_length=300)  # stable locator: "id:<id>" or "name:<name>"
    label: str = Field(max_length=2000)
    kind: Kind
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=500)
    options_complete: bool = True  # False: a huge list, only the first 500 were sent
    accept: str | None = Field(None, max_length=500)  # file inputs


def normalize_label(label: str) -> str:
    """Lowercase a form label and strip asterisks, (optional)/(required) and punctuation; capped at 280 chars."""
    t = label.lower().replace("’", "'")
    t = re.sub(r"[*✱]+", " ", t)
    t = re.sub(r"\(optional\)|\(required\)", " ", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())[:280]


def saved_key(label: str) -> str:
    """Answers-bank key for your answer to this exact question."""
    return "q:" + normalize_label(label)


# (answer key, pattern on the normalized label). First match wins, so specific before general.
STANDARD: list[tuple[str, str]] = [
    ("preferred_name", r"preferred (first )?name|nickname"),
    ("first_name", r"^(legal )?first name|^given name"),
    ("last_name", r"^(legal )?last name|^family name|^surname"),
    ("full_name", r"^(full |legal )?name$|^your name|^full legal name|\bfull (legal )?name\b|what is your name"),
    ("email", r"\be ?mail\b"),
    ("phone", r"\bphone\b|\bmobile\b|\bcell\b"),
    ("linkedin_url", r"linkedin"),
    ("github_url", r"github"),
    ("website_url", r"\bwebsite\b|portfolio|personal site|blog"),
    ("current_company", r"current (company|employer)|^company$|^employer$"),
    ("pronouns", r"\bpronouns?\b"),
    ("hispanic_latino", r"hispanic|latin[oax]"),
    ("gender", r"^gender\b|gender identity"),
    ("race", r"\brace\b|ethnicity"),
    ("veteran_status", r"veteran"),
    ("disability_status", r"disabilit"),
    # Sponsorship has two different answers: needed *in the future* vs needed *now*. A question that
    # doesn't say which ("Do you require sponsorship?") matches neither and comes to you.
    ("requires_sponsorship", r"sponsor(?=.*\bfuture\b)|\bfuture\b.*sponsor"),
    (
        "sponsorship_now",
        r"(\bnow\b|currently|immediately|at this time|to (begin|start))(?!.*\bfuture\b).*sponsor|"
        r"sponsor\w*(?!.*\bfuture\b).*(\b(now|currently|immediately|at this time)\b|to (begin|start))",
    ),
    (
        "work_authorized",
        r"authori[sz]ed to work|work authori[sz]ation|eligible to work|legally (able|permitted) to work",
    ),
    ("previously_employed", r"(ever|previously) (worked|been employed)|worked (for|at) \w+ before|former employee"),
    (
        "how_heard",
        r"how (did )?you (first )?(hear|heard|find|found|learn)|where did you (first )?(hear|find)|referral source|"
        r"^source$",
    ),
    ("work_location", r"where do you intend to work|which (office|location)|preferred (office|location)"),
    (
        "country",
        r"^country\b|country of residence|what country do you (currently )?(live|reside)|country (you )?(currently "
        r")?(live|reside)",
    ),
    ("location", r"^location\b|current location|^city\b|where are you (located|based)"),
    ("school", r"\b(university|school|college)\b"),
    ("degree", r"^degree\b|highest (level of )?education"),
    ("graduation_date", r"graduat"),
    ("gpa", r"\bgpa\b|grade point average"),
    ("start_date", r"start date|when can you start|earliest start"),
    ("sms_consent", r"\bsms\b|text messages"),
    ("whatsapp_consent", r"whatsapp"),
    ("salary_expectations", r"salary|compensation expectation|pay expectation"),
]
_STANDARD = [(k, re.compile(p)) for k, p in STANDARD]

# Questions that always go to you, whatever is in your answers bank:
# checks aimed at bots/AI, puzzles, and anything where your own words matter.
HUMAN_CHECK = re.compile(
    r"are you (a )?(human|robot|bot)|\bai\b|artificial intelligence|language model|chat ?gpt|llm\b|"
    r"captcha|prove (that )?you|secret|encoded|decode|puzzle|riddle|not a (robot|bot)"
)
ATTESTATION = re.compile(
    r"\bcertify\b|\battest\b|\backnowledge\b|\bi agree\b|\bconsent\b|terms (and|&) conditions|privacy (policy|notice)"
)


def standard_key(q: FormQuestion) -> str | None:
    """Which standard answer a form question maps to, or None if unknown or it matches more than one."""
    if q.kind == "file":
        # Labels on file inputs are often blank or just "Attach"; the field's id/name usually says which it is.
        text = normalize_label(q.label) + " " + normalize_label(q.ref.partition(":")[2].replace("_", " "))
        if re.search(r"cover", text):
            return "cover_letter"
        if re.search(r"resume|\bcv\b", text):
            return "resume"
        return None  # unidentified file field: attach nothing (asked only if required)
    label = normalize_label(q.label)
    if q.kind in CHOICE_KINDS and q.options:
        # Consent radios often sit under another field's label (e.g. "Phone"); their options say what they are.
        opts = normalize_label(" ".join(q.options))
        for key in ("sms_consent", "whatsapp_consent"):
            if dict(STANDARD)[key] and re.search(dict(STANDARD)[key], opts):
                return key
    hits = [key for key, pat in _STANDARD if pat.search(label)]
    # A question touching two standard answers ("authorized to work ... without sponsorship") can
    # mean the opposite of either one. Never map it: it goes to you, word for word.
    if len(set(hits)) != 1:
        return None
    return hits[0]


def must_ask(q: FormQuestion) -> str | None:
    """Reason this question can only be answered by you, or None."""
    label = normalize_label(q.label)
    if HUMAN_CHECK.search(label):
        return "a check aimed at bots/AI or a puzzle: answer it yourself"
    if q.kind == "textarea":
        return "a written answer in your own words"
    return None


_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}
_DECLINE = re.compile(
    r"decline|prefer not|don.?t wish|do not wish|not to (say|disclose|answer|self.?identify)|choose not"
)


def match_option(value: str, options: list[str]) -> str | None:
    """Pick the option your answer means, or None if none clearly does. Never guesses."""
    if not options:
        return None
    v = value.strip().lower()
    if not v:
        return None
    lowered = [(o, o.strip().lower()) for o in options]
    for o, lo in lowered:
        if lo == v:
            return o
    if v in _YES or v in _NO:
        want = "yes" if v in _YES else "no"
        hits = [o for o, lo in lowered if re.match(rf"^{want}\b", lo)]
        return hits[0] if len(hits) == 1 else None
    if _DECLINE.search(v):
        hits = [o for o, lo in lowered if _DECLINE.search(lo)]
        return hits[0] if len(hits) == 1 else None
    starts = [o for o, lo in lowered if lo.startswith(v) or v.startswith(lo)]
    if len(starts) == 1:
        return starts[0]
    contains = [o for o, lo in lowered if v in lo]
    return contains[0] if len(contains) == 1 else None
