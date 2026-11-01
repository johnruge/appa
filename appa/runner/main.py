"""`appa runner`: poll your server for apply jobs and do them in your browser.

    export APPA_RUNNER_KEY=appa_...          # `appa keys create my-mac --scopes runner`
    appa runner --server https://appa.example.com --resume ~/Documents/resume.pdf

Prepare: open the form, report its questions, fill per the server's plan, screenshot.
         Then it waits for your approval. Nothing is submitted in this step.
Submit:  only for attempts you approved. Fill again, check every field, submit, look for
         the confirmation page. A CAPTCHA challenge pauses up to 10 minutes for you to solve it.
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
CAPTCHA_WAIT_S = 600
VERIFY_WAIT_S = 600
MAX_SHOT_HEIGHT = 7800


def _log(msg: str) -> None:
    """Print a line immediately."""
    print(msg, flush=True)


def _screenshot(page) -> bytes:
    """What you approve: the application form itself when there is one (some sites show it in
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
        """Open the job's page and run its prepare or submit step under a heartbeat; blocks go back to you."""
        jid, aid, phase = job["job_id"], job["attempt_id"], job["phase"]
        self.log(f"[runner] attempt #{aid}: {phase} {job['apply_url']}")
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(jid, stop), daemon=True)
        beat.start()
        page = None
        try:
            page = self.browser.open(job["apply_url"])
            if job.get("ats") == "generic":
                page = dom.find_form(page, self.browser)  # any site: get to the form, or hand it back
            if phase == "prepare":
                self.prepare(jid, aid, page)
            elif phase == "submit":
                self.submit(jid, aid, page, job["plan"])
            else:
                self.client.fail(jid, f"unknown phase {phase}")
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
            if page:
                page.close()

    def _heartbeat(self, job_id: int, stop: threading.Event) -> None:
        """Send a heartbeat every two minutes until stopped, ignoring errors."""
        while not stop.wait(120):
            try:
                self.client.heartbeat(job_id)
            except Exception:
                pass

    def prepare(self, jid: int, aid: int, page) -> None:
        """Read the form, get the fill plan, fill it, and send the report and a screenshot for approval."""
        questions = dom.extract(page)
        self.client.log(aid, f"read the form: {len(questions)} questions")
        reply = self.client.form(aid, questions, page.url)
        if reply.get("action") != "fill":
            self.client.log(aid, f"stopped: {reply.get('reason')}")
            self.client.done(jid)
            return
        report = dom.fill(page, reply["plan"], self.files)
        bad = [r for r in report if not r["ok"]]
        self.client.log(aid, f"filled {len(report) - len(bad)}/{len(report)} fields")
        upload = self.client.screenshot(aid, _screenshot(page))
        self.client.preview(aid, report, upload)
        self.client.done(jid)

    def submit(self, jid: int, aid: int, page, plan: list[dict]) -> None:
        """Refill the approved plan, wait out CAPTCHAs, submit, and report whether it was confirmed."""
        present = {q["ref"] for q in dom.extract(page)}
        missing = [i["label"] for i in plan if i.get("value") is not None and i["ref"] not in present]
        if missing:
            self.client.result(aid, submitted=False, error=f"form changed since you approved it: {missing[:3]}")
            self.client.done(jid)
            return
        report = dom.fill(page, plan, self.files)
        bad = [r for r in report if not r["ok"]]
        if bad:
            self.client.result(
                aid, submitted=False, error=f"{len(bad)} field(s) didn't fill: {bad[0]['label']}: {bad[0]['error']}"
            )
            self.client.done(jid)
            return
        if not self._wait_out_captcha(aid, page):
            self.client.result(aid, submitted=False, error="CAPTCHA wasn't solved within 10 minutes")
            self.client.done(jid)
            return
        before = page.url
        dom.submit(page)
        page.wait_for_timeout(1500)
        if not self._wait_out_captcha(aid, page):
            self.client.result(aid, submitted=False, error="CAPTCHA after submit wasn't solved within 10 minutes")
            self.client.done(jid)
            return
        confirmed, errors = dom.confirmation(page, before, timeout_s=15)
        if not confirmed and dom.verification_requested(page):
            confirmed = self._wait_for_you_to_verify(aid, page, before)
            if not confirmed:
                upload = self.client.screenshot(aid, _screenshot(page))
                self.client.result(
                    aid,
                    submitted=False,
                    final_url=page.url,
                    screenshot_upload_id=upload,
                    error="the site asked for the verification code it emailed you, and it wasn't "
                    "entered within 10 minutes; the application was NOT submitted",
                )
                self.client.done(jid)
                return
            errors = []
        upload = self.client.screenshot(aid, _screenshot(page))
        if confirmed:
            self.client.result(aid, submitted=True, confirmed=True, final_url=page.url, screenshot_upload_id=upload)
        elif errors:
            self.client.result(
                aid,
                submitted=False,
                final_url=page.url,
                screenshot_upload_id=upload,
                error="the form reported errors: " + "; ".join(errors)[:1500],
            )
        else:
            # Clicked submit, no errors, no recognizable confirmation: count it, and say so.
            self.client.result(aid, submitted=True, confirmed=False, final_url=page.url, screenshot_upload_id=upload)
        self.client.done(jid)

    def _wait_for_you_to_verify(self, aid: int, page, before: str) -> bool:
        """Wait up to 10 minutes for you to enter an emailed code and submit; True once confirmed."""
        msg = (
            "the site emailed you a verification code: enter it in the appa Chrome window and click "
            "Submit there (waiting 10 min)"
        )
        self.client.log(aid, msg)
        self.log(f"[runner] {msg}")
        page.bring_to_front()
        deadline = time.monotonic() + VERIFY_WAIT_S
        while time.monotonic() < deadline:
            confirmed, _ = dom.confirmation(page, before, timeout_s=5)
            if confirmed:
                self.client.log(aid, "verified and submitted")
                return True
        return False

    def _wait_out_captcha(self, aid: int, page) -> bool:
        """If a CAPTCHA shows, wait up to 10 minutes for you to solve it; True once it is gone."""
        if not dom.captcha_visible(page):
            return True
        self.client.log(aid, "CAPTCHA challenge showing: solve it in the appa Chrome window (10 min)")
        self.log("[runner] CAPTCHA: please solve it in the appa Chrome window")
        deadline = time.monotonic() + CAPTCHA_WAIT_S
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            if not dom.captcha_visible(page):
                self.client.log(aid, "CAPTCHA solved, continuing")
                return True
        return False


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
