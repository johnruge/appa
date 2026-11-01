"""Feed tests run offline: `feed_http` serves recorded responses through appa's guarded HTTP module."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from appa.sources import http

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    """A recorded response body from tests/feeds/fixtures."""
    return (FIXTURES / name).read_bytes()


@pytest.fixture()
def feed_http(monkeypatch):
    """Route requests to canned responses: routes[(method, url_prefix)] or routes[url_prefix] -> body/status.

    A value is bytes/str/dict/list (200 response) or an httpx.Response. Every request is recorded in
    `calls` so a test can check what was asked for (query params, POST bodies, headers)."""
    routes: dict = {}
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        url = str(req.url)
        for k, v in sorted(routes.items(), key=lambda kv: -len(kv[0] if isinstance(kv[0], str) else kv[0][1])):
            method, prefix = (None, k) if isinstance(k, str) else k
            if (method is None or method == req.method) and url.startswith(prefix):
                if isinstance(v, httpx.Response):
                    return v
                if isinstance(v, (dict, list)):
                    return httpx.Response(
                        200, content=json.dumps(v).encode(), headers={"content-type": "application/json"}
                    )
                return httpx.Response(200, content=v.encode() if isinstance(v, str) else v)
        return httpx.Response(404, content=b"not found")

    monkeypatch.setattr(http, "check_url", lambda url: None)  # no DNS in tests (check_url has its own tests)
    monkeypatch.setattr(http, "_transport", httpx.MockTransport(handler))
    return _Routes(routes, calls)


class _Routes:
    """Test handle: `r[prefix] = body` adds a route; `r.calls` lists requests made."""

    def __init__(self, routes: dict, calls: list):
        """Wrap the shared route table and the request log."""
        self._routes, self.calls = routes, calls

    def __setitem__(self, k, v) -> None:
        """Add or replace a route."""
        self._routes[k] = v

    def __getitem__(self, k):
        """Read a route back."""
        return self._routes[k]


@pytest.fixture()
def load_fixture():
    """`load_fixture("name.json")` -> the recorded body as bytes (JSON fixtures: use json.loads on it)."""
    return fixture
