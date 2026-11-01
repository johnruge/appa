from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from appa.runtime.context import RunContext


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    Input: type[BaseModel]  # validated before a run is created
    run: Callable[[RunContext, BaseModel], dict]  # returns the run's result summary
    model_role: str = "screener"  # which default in config/models.yaml
    uses_models: bool = True
    startable: bool = True  # False: only created internally (e.g. applier, by the apply service)
    default_budget: dict = field(default_factory=dict)
