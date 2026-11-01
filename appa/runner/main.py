"""`appa runner`: poll your server for apply jobs and do them in your browser.

    export APPA_RUNNER_KEY=appa_...          # `appa keys create my-mac --scopes runner`
    appa runner --server https://appa.example.com --resume ~/Documents/resume.pdf

Prepare: open the form, report its questions, fill per the server's plan, screenshot, and leave the
         filled tab open in the appa Chrome window. You check it and press submit yourself.

The runner never submits anything. There is no code here that clicks a submit button.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from appa.runner import dom
from appa.runner.browser import Browser, NotPublic
from appa.runner.client import RunnerClient

POLL_S = 5
MAX_SHOT_HEIGHT = 7800


def _log(msg: str) -> None:
    """Print a line immediately."""
    print(msg, flush=True)


def _screenshot(page) -> bytes:
    """What you review: the application form itself when there is one (some sites show it in
    an overlay that a full-page shot misses), otherwise the page."""
    try:
        forms = [f for f in page.locator("form").all() if f.locator("input, select, textarea").count() >= 2]
        if forms:
            form = max(forms, key=lambda f: f.locator("input, select, textarea").count())
            box = form.bounding_box()
            if box and box["height"] <= MAX_SHOT_HEIGHT:
                return form.screenshot(timeout=10000)
    except Exception:
        pass
    height = min(page.evaluate("document.documentElement.scrollHeight"), MAX_SHOT_HEIGHT)
    return page.screenshot(full_page=True, clip={"x": 0, "y": 0, "width": 1280, "height": max(height, 400)})


class Runner:
    def __init__(self, client: RunnerClient, browser: Browser, files: dom.Files, log=_log):
        """A runner using this server client, browser and files to attach."""
        self.client, self.browser, self.files, self.log = client, browser, files, log

    def loop(self, once: bool = False) -> None:
        """Poll for apply jobs and handle them, backing off on network errors; exits if the key is rejected."""
        self.log("[runner] waiting for apply jobs (Ctrl-C to stop)")
        backoff = POLL_S
        while True:
            try:
                job = self.client.next()
                backoff = POLL_S
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise SystemExit("the server rejected the runner key (revoked, or missing the runner scope)") from e
                job = None
                self.log(f"[runner] server error {e.response.status_code}; retrying in {backoff}s")
            except httpx.TransportError:
                # Server restarting or network blip: keep waiting instead of exiting.
                self.log(f"[runner] can't reach the server; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if job:
                self.handle(job)
            elif once:
                return
            else:
                time.sleep(POLL_S)

    def handle(self, job: dict) -> None:
        """Open the job's page and prefill it under a heartbeat; the filled tab stays open for you, blocks go
        back to you."""
        jid, aid, phase = job["job_id"], job["attempt_id"], job["phase"]
        self.log(f"[runner] attempt #{aid}: {phase} {job['apply_url']}")
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(jid, stop), daemon=True)
        beat.start()
        page, handed_over = None, False
        try:
            page = self.browser.open(job["apply_url"])
            if job.get("ats") == "generic":
                page = dom.find_form(page, self.browser)  # any site: get to the form, or hand it back
            if phase == "prepare":
                handed_over = self.prepare(jid, aid, page)
            else:
                self.client.fail(jid, f"unknown phase {phase}: this runner only prefills")
        except (dom.Blocked, NotPublic) as e:
            self.log(f"[runner] attempt #{aid}: handing back to you: {e}")
            try:
                self.client.blocked(aid, str(e))
                self.client.done(jid)
            except Exception:
                pass
        except Exception as e:
            self.log(f"[runner] attempt #{aid} failed: {e.__class__.__name__}: {e}")
            try:
                self.client.fail(jid, f"{e.__class__.__name__}: {str(e)[:500]}", retry=phase == "prepare")
            except Exception:
                pass
        finally:
            stop.set()
            if page and not handed_over:
                page.close()

    def _heartbeat(self, job_id: int, stop: threading.Event) -> None:
        """Send a heartbeat every two minutes until stopped, ignoring errors."""
        while not stop.wait(120):
            try:
                self.client.heartbeat(job_id)
            except Exception:
                pass

    def prepare(self, jid: int, aid: int, page) -> bool:
        """Read the form, get the fill plan, fill it, send the report and a screenshot, and hand the tab to you.
        True when the filled tab was left open for you."""
        questions = dom.extract(page)
        self.client.log(aid, f"read the form: {len(questions)} questions")
        reply = self.client.form(aid, questions, page.url)
        if reply.get("action") != "fill":
            self.client.log(aid, f"stopped: {reply.get('reason')}")
            self.client.done(jid)
            return False
        report = dom.fill(page, reply["plan"], self.files)
        bad = [r for r in report if not r["ok"]]
        self.client.log(aid, f"filled {len(report) - len(bad)}/{len(report)} fields")
        upload = self.client.screenshot(aid, _screenshot(page))
        self.browser.hand_over(page)  # yours now: appa won't touch or close this tab
        self.client.preview(aid, report, upload)
        self.client.done(jid)
        self.log(f"[runner] attempt #{aid}: filled and left open for you to check and submit")
        return True


def check_file(path: str | None, what: str) -> str | None:
    """Resolve a resume/cover letter path; exits if it is missing or not PDF/Word. None if not given."""
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"{what} not found: {p}")
    if p.suffix.lower() not in (".pdf", ".docx", ".doc"):
        raise SystemExit(f"{what} must be a PDF or Word file")
    return str(p)
