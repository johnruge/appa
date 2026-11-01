"""The runner's only connection: HTTPS to your appa server, with a runner-scoped key."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx


class RunnerClient:
    def __init__(self, server: str, key: str, *, transport: httpx.BaseTransport | None = None):
        """An HTTP client for the server's runner API; needs https (except localhost) and an appa key."""
        u = urlparse(server)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("server must be https:// (plain http only for localhost)")
        if not key.startswith("appa_"):
            raise ValueError("APPA_RUNNER_KEY doesn't look like an appa key")
        self.http = httpx.Client(
            base_url=server.rstrip("/") + "/api/v1/runner",
            timeout=60,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
        )

    def _post(self, path: str, **kw) -> dict:
        """POST to the runner API and return the JSON reply ({} if empty); raises on HTTP errors."""
        r = self.http.post(path, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def next(self) -> dict | None:
        """Ask for the next job; None if there is none."""
        r = self.http.get("/next")
        r.raise_for_status()
        return None if r.status_code == 204 else r.json()

    def heartbeat(self, job_id: int) -> None:
        """Keep the job's lease alive."""
        self._post(f"/jobs/{job_id}/heartbeat")

    def done(self, job_id: int) -> None:
        """Mark the job done."""
        self._post(f"/jobs/{job_id}/done")

    def fail(self, job_id: int, error: str, retry: bool = False) -> None:
        """Report the job failed, optionally asking for a retry."""
        self._post(f"/jobs/{job_id}/fail", json={"error": error[:2000], "retry": retry})

    def log(self, attempt_id: int, message: str) -> None:
        """Add a log line to the attempt's run."""
        self._post(
            f"/attempts/{attempt_id}/events", json={"events": [{"kind": "log", "payload": {"message": message}}]}
        )

    def form(self, attempt_id: int, questions: list[dict], page_url: str) -> dict:
        """Send the form questions read from the page; returns the server's fill plan or stop reason."""
        return self._post(f"/attempts/{attempt_id}/form", json={"questions": questions, "page_url": page_url})

    def screenshot(self, attempt_id: int, png: bytes) -> int:
        """Upload a PNG screenshot; returns its upload id."""
        return self._post(f"/attempts/{attempt_id}/screenshot", files={"file": ("page.png", png, "image/png")})[
            "upload_id"
        ]

    def preview(self, attempt_id: int, report: list[dict], upload_id: int | None) -> None:
        """Send the fill report and preview screenshot for your approval."""
        self._post(f"/attempts/{attempt_id}/preview", json={"report": report, "screenshot_upload_id": upload_id})

    def blocked(self, attempt_id: int, reason: str) -> None:
        """Hand the attempt back to you, with the reason."""
        self._post(f"/attempts/{attempt_id}/blocked", json={"reason": reason[:500]})
