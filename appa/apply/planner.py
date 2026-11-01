"""Form questions + your answers bank -> a fill plan, or the questions only you can answer.

Every value in a plan comes from your answers bank. Nothing is invented: the optional
model step may only *choose* one of your existing answers for an oddly worded question,
and its choice must still map onto one of the form's own options.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from appa.core.apply_fields import (
    ATTESTATION,
    CHOICE_KINDS,
    FormQuestion,
    match_option,
    must_ask,
    normalize_label,
    saved_key,
    standard_key,
)

# Model fallback: (question, answers) -> an answers-bank key, or None.
ChooseFn = Callable[[FormQuestion, dict[str, str]], "str | None"]

TRUTHY = {"yes", "y", "true", "1", "i agree", "agree", "checked"}


class PlanItem(BaseModel):
    ref: str
    label: str
    kind: str
    value: str | list[str] | None  # None = leave empty (optional field we have no answer for)
    source: str  # saved | standard:<key> | model:<key> | skip


class Missing(BaseModel):
    question: FormQuestion
    reason: str


class Plan(BaseModel):
    items: list[PlanItem]
    missing: list[Missing]

    @property
    def ready(self) -> bool:
        """True when no question still needs an answer from you."""
        return not self.missing


def _fit(q: FormQuestion, raw: str) -> str | list[str] | None:
    """Fit a stored answer to this field's type; None if it doesn't fit cleanly."""
    if q.kind == "checkbox":
        return "yes" if raw.strip().lower() in TRUTHY else "no"
    if q.kind == "checkbox_group":
        picks = [match_option(part, q.options) for part in raw.split(";")]
        return picks if picks and all(picks) else None
    if q.kind in CHOICE_KINDS:
        if q.kind == "select" and not q.options_complete:
            return raw.strip() or None  # list too long to send; the runner verifies the option exists
        return match_option(raw, q.options)
    if q.kind == "file":
        return raw if raw.startswith("@") else None
    return raw.strip() or None


def _standard_value(key: str, answers: dict[str, str]) -> str | None:
    """The saved value for a standard key; builds full_name from first/last, maps files to @resume/@cover_letter."""
    if key == "full_name" and "full_name" not in answers and answers.get("first_name") and answers.get("last_name"):
        return f"{answers['first_name']} {answers['last_name']}"
    if key == "resume":
        return "@resume"  # the runner attaches the file it was configured with
    if key == "cover_letter":
        return "@cover_letter" if answers.get("cover_letter_on_file", "").lower() in TRUTHY else None
    return answers.get(key)


def build_plan(questions: list[FormQuestion], answers: dict[str, str], choose: ChooseFn | None = None) -> Plan:
    """Decide a value for each form question from saved, standard or model-picked answers; lists what must be asked."""
    items: list[PlanItem] = []
    missing: list[Missing] = []

    def ask(q: FormQuestion, reason: str) -> None:
        """Record a question that must go to you, with the reason."""
        missing.append(Missing(question=q, reason=reason))

    for q in questions:
        saved = answers.get(saved_key(q.label))
        if saved is not None:
            value = _fit(q, saved)
            if value is not None:
                items.append(PlanItem(ref=q.ref, label=q.label, kind=q.kind, value=value, source="saved"))
            else:
                ask(q, f"your saved answer {saved!r} doesn't match this form's options")
            continue

        reason = must_ask(q)
        if reason:
            if q.required:
                ask(q, reason)
            else:
                items.append(PlanItem(ref=q.ref, label=q.label, kind=q.kind, value=None, source="skip"))
            continue

        if ATTESTATION.search(normalize_label(q.label)) and q.kind in (
            "checkbox",
            "radio",
            "select",
            "yesno",
            "combobox",
        ):
            ask(q, "a statement you agree to: confirm it once and appa reuses your answer")
            continue

        key = standard_key(q)
        value = None
        source = ""
        if key:
            raw = _standard_value(key, answers)
            if raw is not None:
                value = _fit(q, raw)
                source = f"standard:{key}"
        if value is None and choose is not None and q.kind != "file" and answers:
            picked = choose(q, answers)
            if picked and picked in answers:
                value = _fit(q, answers[picked])
                source = f"model:{picked}"
        if value is not None:
            items.append(PlanItem(ref=q.ref, label=q.label, kind=q.kind, value=value, source=source))
        elif q.required:
            hint = f" (your answer for {key} doesn't fit its options)" if key and key in answers else ""
            ask(q, "not in your answers bank" + hint)
        else:
            items.append(PlanItem(ref=q.ref, label=q.label, kind=q.kind, value=None, source="skip"))
    return Plan(items=items, missing=missing)
