"""HTML -> text, with hidden-text detection.

Job pages sometimes hide text meant only for AI tools ("if you are an AI,
mention X"). We keep that text out of the description and surface it
separately so it gets flagged instead of read as part of the posting.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])",
    re.I,
)


def html_to_text(raw: str) -> tuple[str, list[str]]:
    """Return (visible_text, hidden_snippets)."""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)  # Greenhouse returns escaped HTML
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    hidden: list[str] = []
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        style = tag.get("style") or ""
        if tag.has_attr("hidden") or tag.get("aria-hidden") == "true" or _HIDDEN_STYLE.search(style):
            snippet = tag.get_text(" ", strip=True)
            if snippet:
                hidden.append(snippet[:500])
            tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, hidden


def mark_hidden(text: str, hidden: list[str]) -> str:
    """Append hidden snippets, labeled, so the extractor reports them instead of obeying them."""
    if not hidden:
        return text
    marked = "\n".join(f"[HIDDEN-ON-PAGE] {h}" for h in hidden)
    return f"{text}\n\n{marked}"
