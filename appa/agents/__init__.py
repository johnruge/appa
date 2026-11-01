"""Agent roles. A role is instructions + tools + a default model + budgets; starting one
creates a run (appa/runtime). Add a role by writing a RoleSpec and registering it here."""

from __future__ import annotations

from appa.agents.base import RoleSpec


def _roles() -> dict[str, RoleSpec]:
    """Every role keyed by name (imported lazily to avoid an import cycle)."""
    from appa.agents import applier, concierge, scout, screener

    return {r.name: r for r in (scout.ROLE, screener.ROLE, concierge.ROLE, applier.ROLE)}


def get_role(name: str) -> RoleSpec:
    """Look up a role by name; raises ValueError listing the available roles if it is unknown."""
    roles = _roles()
    if name not in roles:
        raise ValueError(f"unknown role {name!r}; available: {', '.join(roles)}")
    return roles[name]


def all_roles(startable_only: bool = True) -> list[RoleSpec]:
    """All roles, by default only the ones a user can start directly."""
    return [r for r in _roles().values() if r.startable or not startable_only]
