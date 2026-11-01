"""Applier: prefills an application in your browser and leaves it open for you to submit.

Its steps run on the runner (appa/runner/), not on a worker; appa/apply/service.py moves
each attempt along and records everything on this run.
"""

from __future__ import annotations

from pydantic import BaseModel

from appa.agents.base import RoleSpec


class ApplierInput(BaseModel):
    application_id: int


def run(ctx, params: ApplierInput) -> dict:
    """Always raises: applier runs are done by the user's browser runner, never by a worker."""
    raise RuntimeError("applier runs are executed by your browser runner, not by a worker")


ROLE = RoleSpec(
    name="applier",
    description="Prefill an application in your browser; you check it and submit it yourself.",
    Input=ApplierInput,
    run=run,
    startable=False,
    default_budget={"max_usd": 0.10, "max_steps": 500, "max_model_calls": 50},
)
