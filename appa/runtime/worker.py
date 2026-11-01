"""Worker: claims queued runs and executes them. Run as many as you like; the queue
guarantees each run goes to exactly one worker.

    appa worker            # loop until Ctrl-C / SIGTERM (finishes the current run first)
    appa worker --once     # run at most one job, then exit (tests, cron)
"""

from __future__ import annotations

import os
import select as _select
import signal
import socket
import threading

from sqlalchemy.orm import Session, sessionmaker

from appa import db
from appa.runtime import queue, runs
from appa.runtime.context import is_paused

IDLE_WAIT_S = 5.0  # max sleep between polls; NOTIFY wakes us sooner


class Worker:
    def __init__(self, sessions: sessionmaker[Session] | None = None, *, daily_cap_usd: float | None = None, log=None):
        """A worker named host:pid that runs queued jobs with its own sessions."""
        self.sessions = sessions or db.sessions()
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.daily_cap_usd = daily_cap_usd
        self.stopping = threading.Event()
        self.log = log or (lambda msg: print(msg, flush=True))

    def run_one(self) -> bool:
        """Claim and execute one job. Returns False if there was nothing to do."""
        with self.sessions() as s:
            if is_paused(s):
                return False
            job = queue.claim(s, self.name)
            if not job:
                return False
            job_id, run_id = job.id, job.run_id
        self.log(f"[worker] run #{run_id} claimed (job {job_id})")
        heartbeat = threading.Thread(target=self._heartbeat, args=(job_id,), daemon=True)
        done = threading.Event()
        heartbeat.done = done  # type: ignore[attr-defined]
        heartbeat.start()
        try:
            run = runs.execute(self.sessions, run_id, daily_cap_usd=self.daily_cap_usd)
            with self.sessions() as s:
                queue.done(s, s.get(db.QueueJob, job_id))
            self.log(
                f"[worker] run #{run_id} {run.status}"
                + (f": {run.error}" if run.error else "")
                + f" (${run.cost_usd:.4f})"
            )
        except Exception as e:  # infrastructure failure (DB down...): retry later
            with self.sessions() as s:
                queue.fail(s, s.get(db.QueueJob, job_id), f"{e.__class__.__name__}: {e}", retry=True)
            self.log(f"[worker] job {job_id} failed, will retry: {e.__class__.__name__}")
        finally:
            done.set()
        return True

    def _heartbeat(self, job_id: int) -> None:
        """Keep the job's lease alive from a side thread until the thread is marked done."""
        me = threading.current_thread()
        while not me.done.wait(timeout=queue.LEASE.total_seconds() / 3):  # type: ignore[attr-defined]
            with self.sessions() as s:
                job = s.get(db.QueueJob, job_id)
                if job and job.status == "leased":
                    queue.extend(s, job)

    def loop(self) -> None:
        """Run jobs until SIGTERM or SIGINT, sleeping until notified (or a timeout) when idle."""
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
        self.log(f"[worker] {self.name} started; waiting for runs")
        with _Listener(self.sessions) as wake:
            while not self.stopping.is_set():
                if not self.run_one():
                    wake.wait(IDLE_WAIT_S, self.stopping)
        self.log("[worker] stopped")


class _Listener:
    """LISTEN on the queue channel so idle workers react to new jobs immediately."""

    def __init__(self, sessions: sessionmaker[Session]):
        """A LISTEN connection for new-job notifications, opened on enter."""
        self.sessions = sessions
        self.conn = None

    def __enter__(self):
        """Open a raw connection and LISTEN for new jobs; on failure, fall back to polling."""
        try:
            self.conn = self.sessions.kw["bind"].raw_connection()
            self.conn.driver_connection.autocommit = True
            assert queue.CHANNEL == "appa_jobs"
            self.conn.driver_connection.execute("LISTEN appa_jobs")
        except Exception:
            self.conn = None  # fall back to polling
        return self

    def wait(self, timeout: float, stopping: threading.Event) -> None:
        """Wait up to `timeout` for a job notification (or just sleep when polling), draining notifications."""
        if self.conn is None:
            stopping.wait(timeout)
            return
        pg = self.conn.driver_connection
        try:
            ready, _, _ = _select.select([pg.fileno()], [], [], timeout)
            if ready:
                for _ in pg.notifies(timeout=0):
                    pass
        except Exception:
            stopping.wait(timeout)

    def __exit__(self, *exc):
        """Close the listening connection."""
        if self.conn is not None:
            self.conn.close()
