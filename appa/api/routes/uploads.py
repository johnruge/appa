"""Upload a screenshot; get an id to pass to a Scout run as input.upload_ids."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from appa import uploads
from appa.api import auth
from appa.api.deps import SessionDep, UserDep

router = APIRouter(prefix="/uploads", tags=["uploads"])
upload_limiter = auth.RateLimiter(limit=20)  # uploads per user per minute


@router.post("", status_code=201)
def upload(s: SessionDep, p: UserDep, file: UploadFile = File(...)):
    """Upload a screenshot (rate-limited per user, validated by save_image); returns its metadata."""
    if not upload_limiter.allow(f"user:{p.user.id}"):
        raise HTTPException(429, "too many uploads; wait a minute", headers={"Retry-After": "60"})
    data = file.file.read(uploads.MAX_BYTES + 1)
    try:
        row = uploads.save_image(s, data, p.user.id)
    except uploads.BadUpload as e:
        raise HTTPException(413 if "larger than" in str(e) and "MB" in str(e) else 422, str(e)) from e
    return {"id": row.id, "mime": row.mime, "bytes": row.bytes, "width": row.width, "height": row.height}
