"""Job IDs a page states about itself: JSON-LD JobPosting identifiers, canonical links,
and job-ID attributes. These come from the page's own markup, so no guessing is involved."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from appa.core.jobid import clean_id, job_key

ID_ATTRS = ("data-job-id", "data-jobid", "data-requisition-id", "data-req-id", "data-posting-id")
ID_META = ("job-id", "jobid", "job_id", "requisition-id", "og:job_id")


def _ld_ids(node) -> list[str]:
    """Recursively collect JobPosting identifier values from JSON-LD data."""
    out: list[str] = []
    if isinstance(node, list):
        for n in node:
            out += _ld_ids(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            ident = node.get("identifier")
            for i in ident if isinstance(ident, list) else [ident]:
                v = i.get("value") if isinstance(i, dict) else i
                if isinstance(v, (str, int)):
                    out.append(str(v))
        for k in ("@graph", "mainEntity"):
            if k in node:
                out += _ld_ids(node[k])
    return out


def find_in_page(url: str, html: str) -> tuple[str | None, str | None]:
    """(job_id, how it was found) or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for raw in _ld_ids(data):
            if jid := clean_id(raw):
                return jid, "page:json-ld"
    for sel in ('link[rel="canonical"]', 'meta[property="og:url"]'):
        tag = soup.select_one(sel)
        href = tag and (tag.get("href") or tag.get("content"))
        if href and href != url and (key := job_key(href)):
            return key.partition(":")[2], "page:canonical"
    for name in ID_META:
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and (jid := clean_id(tag.get("content"))):
            return jid, "page:meta"
    for attr in ID_ATTRS:
        tag = soup.find(attrs={attr: True})
        if tag and (jid := clean_id(tag.get(attr))):
            return jid, "page:attribute"
    return None, None
