"""A dedicated, visible Chrome profile, pinned to the application site.

- Separate from your personal browsing (its own profile directory).
- Opens public sites only: a host that resolves to a private or local address (your router,
  other devices on your Wi-Fi, localhost) is refused, so a hostile link can't point your
  browser at your own network.
- Every top-level navigation is checked: the tab may only move within the application's own
  site (its domain and subdomains) and the ATS domains. Pop-ups are closed, downloads refused.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, Route, sync_playwright

ATS_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com")
# Hosted application systems a company site may hand you to (and which work without a login).
ATS_DOMAINS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "bamboohr.com",
    "jobvite.com",
    "recruitee.com",
    "breezy.hr",
    "teamtailor.com",
    "applytojob.com",
    "rippling.com",
    "dover.com",
    "pinpointhq.com",
    "personio.com",
    "careerpuck.com",
    "gem.com",
    "wellfound.com",
)
_TWO_PART_TLDS = ("co.uk", "com.au", "co.in", "co.jp", "com.br", "co.za", "com.sg")


def site_of(host: str) -> str:
    """Registrable domain, roughly: careers.acme.com -> acme.com, jobs.acme.co.uk -> acme.co.uk."""
    parts = host.lower().strip(".").split(".")
    n = 3 if ".".join(parts[-2:]) in _TWO_PART_TLDS else 2
    return ".".join(parts[-n:])


class NotPublic(ValueError):
    pass


def check_public(url: str) -> str:
    """The host, if the URL is http(s) to a public address; raise otherwise."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise NotPublic(f"refusing to open {url[:80]!r}: not an http(s) link")
    try:
        infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise NotPublic(f"can't resolve {host}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise NotPublic(f"refusing to open {host}: it points to a private or local address")
    return host


DEFAULT_PROFILE = Path.home() / "Library" / "Application Support" / "appa" / "chrome-profile"


class Browser:
    def __init__(self, profile_dir: Path = DEFAULT_PROFILE, *, headless: bool = False):
        """A browser using a persistent profile directory; starts allowing only ATS hosts."""
        self.profile_dir = profile_dir
        self.headless = headless
        self._pw: Playwright | None = None
        self.ctx: BrowserContext | None = None
        self.allowed: set[str] = set(ATS_HOSTS)
        self.allowed_sites: set[str] = set()
        self.blocked: list[str] = []
        self.check_ips = True  # tests serving fake local sites turn this off

    def __enter__(self) -> Browser:
        """Launch your installed Chrome (or bundled Chromium) with the navigation guard and popup blocker."""
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._pw = sync_playwright().start()
        launch = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **launch)  # your installed Chrome
        except Exception:
            self.ctx = self._pw.chromium.launch_persistent_context(**launch)  # bundled Chromium
        self.ctx.route("**/*", self._guard)
        self.ctx.on("page", self._no_popups)
        return self

    def __exit__(self, *exc) -> None:
        """Close the browser and stop Playwright."""
        if self.ctx:
            self.ctx.close()
        if self._pw:
            self._pw.stop()

    def allows(self, host: str) -> bool:
        """True if the host is allowed or is under one of the allowed sites."""
        host = host.lower()
        return host in self.allowed or any(host == d or host.endswith("." + d) for d in self.allowed_sites)

    def _guard(self, route: Route) -> None:
        """Abort top-level navigations to hosts that aren't allowed or don't resolve to public IPs."""
        req = route.request
        if req.is_navigation_request() and req.frame.parent_frame is None:
            host = (urlparse(req.url).hostname or "").lower()
            ok = self.allows(host)
            if ok and self.check_ips:
                try:
                    check_public(req.url)
                except NotPublic:
                    ok = False
            if not ok:
                self.blocked.append(req.url[:200])
                route.abort()
                return
        route.continue_()

    def _no_popups(self, page: Page) -> None:
        """Close any page opened by another page (window.open, target=_blank)."""
        # Pages opened *by* a page (window.open, target=_blank) are closed; our own new_page() has no opener.
        if page.opener() is not None:
            page.close()

    def open(self, url: str) -> Page:
        """Open a public URL in a new page, allowing only its site and hosted ATSs, and wait for it to settle."""
        host = check_public(url) if self.check_ips else (urlparse(url).hostname or "").lower()
        self.allowed = set(ATS_HOSTS) | {host}
        self.allowed_sites = {site_of(host), *ATS_DOMAINS}  # the company's own site + hosted ATSs
        self.blocked = []
        page = self.ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return page
