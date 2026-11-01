"""Run Alembic migrations from code (CLI `appa migrate`, tests)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from appa.config import ROOT


def _cfg(url: str | None) -> Config:
    """An Alembic config for this repo's migrations, optionally pointed at a specific database URL."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False
    if url:
        cfg.attributes["url"] = url
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    """Apply migrations up to the given revision (head by default)."""
    command.upgrade(_cfg(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    """Roll migrations back to the given revision (base by default)."""
    command.downgrade(_cfg(url), revision)
