"""The only way appa makes outbound HTTP requests.

Links come from untrusted places (job alerts, screenshots, pages that redirect),
so every request, and every redirect hop, is checked before connecting:
- http/https only, default ports only
- the hostname must resolve to public addresses only (no localhost, private
  ranges, or cloud metadata like 169.254.169.254)
- response bodies are capped so a hostile page can't exhaust memory

Residual risk: DNS rebinding (the name resolves differently between our check
and the connect). Closing that needs IP pinning at the transport level; do it
before running this as a multi-user hosted service.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urljoin, urlparse

import httpx

TIMEOUT = httpx.Timeout(60.0, connect=15.0)  # some job boards return several MB
HEADERS = {"User-Agent": "appa-job-intake/0.1 (personal job search tool)"}
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_FEED_BYTES = 50 * 1024 * 1024  # whole-company job feeds (e.g. Lever) can be large
BLOCKED_HOSTS = ("linkedin.com",)  # LinkedIn's terms forbid automated access; use screenshots


_transport: httpx.BaseTransport | None = None  # tests inject a mock transport here


class BlockedURL(httpx.RequestError):
    pass


def check_url(url: str) -> None:
    """Raise BlockedURL unless the URL is http(s) on a standard port to a public, non-blocked host."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise BlockedURL(f"only http(s) links are fetched, not {p.scheme or 'no scheme'}")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise BlockedURL("link has no host")
    if p.port not in (None, 80, 443):
        raise BlockedURL(f"non-standard port {p.port} not allowed")
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        raise BlockedURL(f"{host} is not fetched automatically; send a screenshot instead")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"cannot resolve {host}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise BlockedURL(f"{host} resolves to a non-public address")


def _get(
    url: str,
    params: dict | None,
    max_bytes: int,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
) -> tuple[bytes, str]:
    """GET (or a JSON POST) with per-hop checks. Returns (body, final_url). POSTs never follow redirects."""
    extra = dict(headers or {})

    def _origin(u: str) -> tuple:
        """Scheme, host and port: caller headers are re-sent only to exactly this origin."""
        p = urlparse(u)
        return p.scheme, (p.hostname or "").lower(), p.port

    origin = _origin(url)
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=False, transport=_transport) as c:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            # Caller headers (API keys) go only to the host they were meant for, never to a redirect target.
            hop = extra if _origin(url) == origin else {}
            with c.stream(method, url, params=params, json=body, headers=hop) as r:
                if r.is_redirect:
                    if method != "GET":
                        raise BlockedURL("a POST was redirected; not followed")
                    url = urljoin(str(r.url), r.headers.get("location", ""))
                    params = None  # already encoded into the first URL
                    continue
                r.raise_for_status()
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise BlockedURL(f"response larger than {max_bytes} bytes")
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise BlockedURL(f"response larger than {max_bytes} bytes")
                return bytes(data), str(r.url)
    raise BlockedURL("too many redirects")


def get_text(url: str, params: dict | None = None) -> tuple[str, str]:
    """Fetch a page as text (size-limited); returns the text and the final URL."""
    body, final = _get(url, params, MAX_PAGE_BYTES)
    return body.decode("utf-8", errors="replace"), final


def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    """Fetch and parse a JSON feed (size-limited). `headers` adds to the defaults (e.g. an API key)."""
    body, _ = _get(url, params, MAX_FEED_BYTES, headers=headers)
    return json.loads(body)


def post_json(url: str, body: dict, params: dict | None = None, headers: dict | None = None):
    """POST a JSON body and parse the JSON reply (size-limited; redirects are refused). For search APIs
    that only take POST, like Workday's public job search."""
    data, _ = _get(url, params, MAX_FEED_BYTES, method="POST", body=body, headers=headers)
    return json.loads(data)


def get_bytes(url: str, params: dict | None = None, headers: dict | None = None) -> bytes:
    """Fetch a raw feed (XML, RSS) as bytes (size-limited)."""
    return _get(url, params, MAX_FEED_BYTES, headers=headers)[0]
