"""Fallback for links that aren't a known ATS URL (company careers pages, aggregators)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from appa.core.models import Job
from appa.sources import ats
from appa.sources.http import get_text
from appa.sources.page_ids import find_in_page
from appa.sources.text import html_to_text, mark_hidden


def _gh_jid_job(url: str) -> Job | None:
    """careers.<company>.com/...?gh_jid=123 is a Greenhouse job; guess the board slug from the domain."""
    ref = ats.parse_url(url)
    if not ref or ref.ats != "greenhouse" or ref.slug:
        return None
    host = urlparse(url).hostname or ""
    parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "com", "io", "co", "ai", "org")]
    for slug in dict.fromkeys(parts):
        try:
            job = ats.greenhouse_job(slug, ref.job_id)
        except httpx.HTTPError:
            continue
        job.url = url
        return job
    return None


def fetch(url: str) -> Job:
    """Fetch any posting page, preferring an embedded ATS posting, else page metadata, text and a found job ID."""
    if job := _gh_jid_job(url):
        return job
    page, _final = get_text(url)

    # Many careers pages embed an ATS; prefer its structured data when we can find it.
    for found in re.findall(
        r"https?://(?:job-boards|boards|jobs)\.(?:greenhouse\.io|lever\.co|ashbyhq\.com)/[^\s\"'<>]+", page
    ):
        ref = ats.parse_url(found)
        if ref and ref.job_id and ref.slug:
            try:
                job = ats.fetch_job(ref)
                job.url = url
                return job
            except (httpx.HTTPError, LookupError):
                pass

    soup = BeautifulSoup(page, "html.parser")

    def meta(prop: str) -> str:
        """The content of a meta tag by property or name, or ''."""
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    company = meta("og:site_name") or urlparse(url).hostname.removeprefix("www.").split(".")[0]
    text, hidden = html_to_text(page)
    jid, how = (None, None) if ats.parse_url(url) else find_in_page(url, page)
    return Job(
        company=company,
        title=title,
        url=url,
        description=mark_hidden(text[:40000], hidden),
        ats="unknown",
        posting_id=jid,
        posting_id_source=how,
    )
