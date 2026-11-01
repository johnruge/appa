"""Health, and what the UI needs to build its menus (roles, models)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from appa.api.deps import Principal, require

router = APIRouter(tags=["meta"])


@router.get("/health")
def health():
    """Intentionally public: liveness only, reveals nothing."""
    return {"ok": True}


@router.get("/roles")
def roles(_p: Annotated[Principal, Depends(require("runs:read"))]):
    """List the startable agent roles with their budgets and input schemas."""
    from appa.agents import all_roles

    return {
        "roles": [
            {
                "name": r.name,
                "description": r.description,
                "default_budget": r.default_budget,
                "uses_models": r.uses_models,
                "input_schema": r.Input.model_json_schema(),
            }
            for r in all_roles()
        ]
    }


@router.get("/models")
def models(_p: Annotated[Principal, Depends(require("runs:read"))]):
    """List the allowed models, their prices, and the per-role defaults."""
    from appa.llm import registry as reg

    r = reg.get_registry()
    return {
        "defaults": r.defaults,
        "models": [
            {"name": m.name, "vision": m.vision, "price_per_mtok": [m.price_in, m.price_out]} for m in r.models.values()
        ],
    }
