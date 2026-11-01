"""Your profile.md (judgment) and policy (hard rules), versioned in the database.

The newest saved version of each is in effect. Until you save one (from the UI or API),
the files are used: config/profile.md and config/profile.yaml.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from appa import config as config_mod
from appa import db

PROFILE_FILE = config_mod.ROOT / "config" / "profile.md"
MAX_PROFILE_CHARS = 50_000


def _latest(s: Session, kind: str) -> db.ProfileVersion | None:
    """The newest stored version of this kind, or None."""
    return s.scalars(
        select(db.ProfileVersion).where(db.ProfileVersion.kind == kind).order_by(db.ProfileVersion.id.desc()).limit(1)
    ).first()


def get_policy(s: Session) -> config_mod.Config:
    """The newest stored policy, falling back to the YAML config file."""
    row = _latest(s, "policy")
    return config_mod.Config.model_validate(row.content) if row else config_mod.load()


def put_policy(s: Session, data: dict, by: str) -> config_mod.Config:
    """Validate and store a new policy version; commits."""
    cfg = config_mod.Config.model_validate(data)  # raises ValidationError on bad input
    s.add(db.ProfileVersion(kind="policy", content=cfg.model_dump(mode="json"), created_by=by))
    s.commit()
    return cfg


def get_profile_md(s: Session) -> str | None:
    """The newest stored profile text, falling back to the profile file, or None."""
    row = _latest(s, "profile_md")
    if row:
        return row.content.get("text") or None
    return PROFILE_FILE.read_text() if PROFILE_FILE.exists() else None


def put_profile_md(s: Session, text: str, by: str) -> int:
    """Store a new profile version (length-limited) and commit; returns its id."""
    if len(text) > MAX_PROFILE_CHARS:
        raise ValueError(f"profile.md is limited to {MAX_PROFILE_CHARS} characters")
    row = db.ProfileVersion(kind="profile_md", content={"text": text}, created_by=by)
    s.add(row)
    s.commit()
    return row.id


def versions(s: Session, kind: str) -> int:
    """How many versions of this kind are stored."""
    return s.scalar(select(func.count()).select_from(db.ProfileVersion).where(db.ProfileVersion.kind == kind))
