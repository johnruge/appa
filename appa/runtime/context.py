"""RunContext: what a role gets while it runs. Every action goes through it, so every
action is recorded (run_events) and checked against the run's budget and cancel flag.

Events are written in their own short transactions, so they are visible to `appa run`
(and later the web UI) the moment they happen, independent of the role's own work.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from appa import db, llm

EVENTS_CHANNEL = "appa_run_events"
DEFAULT_BUDGET = {"max_usd": 0.50, "max_steps": 500, "max_model_calls": 200}


class StopRun(Exception):
    """Raised inside a role to stop it: cancel requested, budget hit, or system paused."""


class RunContext:
    def __init__(self, sessions: sessionmaker[Session], run: db.Run, *, daily_cap_usd: float | None = None):
        """A context for one run, starting from its saved budget and counters."""
        self.sessions = sessions
        self.run_id = run.id
        self.role = run.role
        self.model = run.model
        self.budget = {**DEFAULT_BUDGET, **(run.budget or {})}
        self.cost_usd = run.cost_usd or 0.0
        self.steps = run.steps or 0
        self.model_calls = run.model_calls or 0
        self.daily_cap_usd = daily_cap_usd
        self._last_flag_check = 0.0

    # ---- recording ----------------------------------------------------------------

    def event(self, kind: str, /, **payload: Any) -> None:
        """Append an event to the run log, notify listeners via pg_notify, and commit."""
        with self.sessions() as s:
            ev = db.RunEvent(run_id=self.run_id, kind=kind, payload=_jsonable(payload))
            s.add(ev)
            s.flush()
            s.execute(text("SELECT pg_notify(:c, :p)"), {"c": EVENTS_CHANNEL, "p": f"{self.run_id}:{ev.id}"})
            s.commit()

    def log(self, message: str, **data: Any) -> None:
        """Append a log message event."""
        self.event("log", message=message, **data)

    def _save_counters(self) -> None:
        """Write the cost, step and model-call counters back to the run row."""
        with self.sessions() as s:
            run = s.get(db.Run, self.run_id)
            run.cost_usd, run.steps, run.model_calls = self.cost_usd, self.steps, self.model_calls
            s.commit()

    # ---- guards -------------------------------------------------------------------

    def check(self) -> None:
        """Stop if cancelled, paused, or over budget. Cheap: DB flags are read at most once a second."""
        b = self.budget
        if self.cost_usd >= b["max_usd"]:
            raise StopRun(f"budget: spent ${self.cost_usd:.4f} of max ${b['max_usd']}")
        if self.steps >= b["max_steps"]:
            raise StopRun(f"budget: {self.steps} steps of max {b['max_steps']}")
        if self.model_calls >= b["max_model_calls"]:
            raise StopRun(f"budget: {self.model_calls} model calls of max {b['max_model_calls']}")
        if time.monotonic() - self._last_flag_check < 1.0:
            return
        self._last_flag_check = time.monotonic()
        with self.sessions() as s:
            if s.scalar(select(db.Run.cancel_requested).where(db.Run.id == self.run_id)):
                raise StopRun("cancelled")
            if is_paused(s):
                raise StopRun("system paused (appa stop-all)")
            if self.daily_cap_usd is not None:
                spent = spent_today(s)
                if spent >= self.daily_cap_usd:
                    raise StopRun(f"daily spend cap reached (${spent:.4f} of ${self.daily_cap_usd})")

    # ---- actions ------------------------------------------------------------------

    def tool(
        self,
        name: str,
        fn: Callable[..., Any],
        /,
        *args: Any,
        summarize: Callable[[Any], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run one tool call as a counted, recorded step."""
        self.check()
        self.steps += 1
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            self.event(
                "tool_call",
                tool=name,
                args=_brief(args, kwargs),
                ok=False,
                error=f"{e.__class__.__name__}: {str(e)[:300]}",
                seconds=round(time.monotonic() - started, 2),
            )
            self._save_counters()
            raise
        self.event(
            "tool_call",
            tool=name,
            args=_brief(args, kwargs),
            ok=True,
            result=summarize(result) if summarize else None,
            seconds=round(time.monotonic() - started, 2),
        )
        self._save_counters()
        return result

    def charge(
        self,
        completion: llm.Completion | None = None,
        *,
        cost_usd: float = 0.0,
        purpose: str = "",
        error: str | None = None,
    ) -> None:
        """Record a model call that already happened (successful or not) and its cost."""
        self.model_calls += 1
        if completion is not None:
            self.cost_usd += completion.cost_usd
            self.event(
                "model_call",
                purpose=purpose,
                model=completion.model,
                served_by=completion.served_by,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cost_usd=round(completion.cost_usd, 6),
                seconds=completion.latency_s,
                attempts=completion.attempts,
            )
        else:
            self.cost_usd += cost_usd
            self.event(
                "model_call",
                purpose=purpose,
                model=self.model,
                ok=False,
                error=(error or "")[:300],
                cost_usd=round(cost_usd, 6),
            )
        self._save_counters()

    def ask(
        self, prompt: str, *, kind: str = "review", application_id: int | None = None, context: dict | None = None
    ) -> int:
        """Leave a question for you. Non-blocking: the run keeps going."""
        with self.sessions() as s:
            q = db.Question(
                run_id=self.run_id,
                application_id=application_id,
                kind=kind,
                prompt=prompt[:4000],
                context=_jsonable(context or {}),
            )
            s.add(q)
            s.commit()
            qid = q.id
        self.event("question", question_id=qid, kind=kind, prompt=prompt[:500])
        return qid


# ---- system switches & spend ----------------------------------------------------------


def is_paused(s: Session) -> bool:
    """True if the kill switch (the 'paused' setting) is on."""
    row = s.get(db.Setting, "paused")
    return bool(row and row.value.get("on"))


def set_paused(s: Session, on: bool) -> None:
    """Turn the kill switch on or off and commit."""
    row = s.get(db.Setting, "paused")
    if row:
        row.value = {"on": on}
    else:
        s.add(db.Setting(key="paused", value={"on": on}))
    s.commit()


def spent_today(s: Session) -> float:
    """Total cost of runs created in the last 24 hours, in USD."""
    since = datetime.now(UTC) - timedelta(hours=24)
    return float(
        s.scalar(select(func.coalesce(func.sum(db.Run.cost_usd), 0.0)).where(db.Run.created_at >= since)) or 0.0
    )


def _brief(args: tuple, kwargs: dict) -> dict:
    """A tool call's arguments as a dict of short loggable values."""
    out = {f"arg{i}": _short(a) for i, a in enumerate(args)}
    out.update({k: _short(v) for k, v in kwargs.items()})
    return out


def _short(v: Any) -> Any:
    """Numbers, bools and None as-is; anything else as a string cut to 200 chars."""
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return str(v)[:200]


def _jsonable(v: Any) -> Any:
    """Recursively convert a value to JSON-safe types, stringifying anything else."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
