"""Edit profile.md (judgment) and the policy (hard rules). Every save is a new version."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from appa import profile_store
from appa.api.deps import SessionDep, UserDep

router = APIRouter(tags=["profile"])


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=profile_store.MAX_PROFILE_CHARS)


@router.get("/profile")
def get_profile(s: SessionDep, _p: UserDep):
    """Return the profile markdown and its version history."""
    return {"text": profile_store.get_profile_md(s) or "", "versions": profile_store.versions(s, "profile_md")}


@router.put("/profile")
def put_profile(body: ProfileIn, s: SessionDep, p: UserDep):
    """Save a new version of the profile markdown."""
    vid = profile_store.put_profile_md(s, body.text, p.label)
    return {"ok": True, "version_id": vid}


@router.get("/policy")
def get_policy(s: SessionDep, _p: UserDep):
    """Return the filtering policy and its version history."""
    return {
        "policy": profile_store.get_policy(s).model_dump(mode="json"),
        "versions": profile_store.versions(s, "policy"),
    }


@router.put("/policy")
def put_policy(body: dict, s: SessionDep, p: UserDep):
    """Validate and save a new policy version; 422 names the first invalid field."""
    try:
        cfg = profile_store.put_policy(s, body, p.label)
    except ValidationError as e:
        first = e.errors()[0]
        raise HTTPException(422, f"invalid policy: {'.'.join(map(str, first['loc']))}: {first['msg']}") from e
    return {"policy": cfg.model_dump(mode="json")}
