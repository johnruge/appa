"""Screenshot uploads: validate from content, re-encode, store privately.

- The type is decided by decoding the bytes, never by filename or Content-Type.
- PNG, JPEG and WebP only, at most 10 MB and 8000 x 8000 px; decompression bombs are refused.
- Every image is re-encoded to a fresh PNG. That drops EXIF/GPS and other metadata and
  neutralizes polyglot files (an image that is also valid HTML/JS/ZIP).
- Stored as data/uploads/<sha256>.png, mode 0600, outside any web root, never served back raw.
"""

from __future__ import annotations

import hashlib
import io
import os
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from appa import db
from appa.config import ROOT

MAX_BYTES = 10 * 1024 * 1024
MAX_SIDE = 8000
ALLOWED = {"PNG", "JPEG", "WEBP"}
Image.MAX_IMAGE_PIXELS = MAX_SIDE * MAX_SIDE


class BadUpload(ValueError):
    pass


def upload_dir() -> Path:
    """The upload directory (APPA_UPLOAD_DIR or data/uploads), created owner-only if missing."""
    d = Path(os.environ.get("APPA_UPLOAD_DIR") or ROOT / "data" / "uploads")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def save_image(s: Session, data: bytes, user_id: int | None = None) -> db.Upload:
    """Validate an image (size, PNG/JPEG/WebP, dimensions), re-encode it as metadata-free PNG, store it; commits."""
    if len(data) > MAX_BYTES:
        raise BadUpload(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise BadUpload("empty file")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                fmt = probe.format
                if fmt not in ALLOWED:
                    raise BadUpload(f"only PNG, JPEG or WebP images are accepted (got {fmt or 'unknown'})")
                if probe.width > MAX_SIDE or probe.height > MAX_SIDE:
                    raise BadUpload(f"image is larger than {MAX_SIDE}x{MAX_SIDE} pixels")
                probe.verify()  # structural check; probe is unusable after this
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                clean = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    except BadUpload:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as e:
        raise BadUpload("not a valid image") from e

    out = io.BytesIO()
    clean.save(out, format="PNG", optimize=True)  # no exif/info passed: metadata is dropped
    png = out.getvalue()
    digest = hashlib.sha256(png).hexdigest()
    path = upload_dir() / f"{digest}.png"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(png)
    row = db.Upload(
        sha256=digest,
        mime="image/png",
        bytes=len(png),
        width=clean.width,
        height=clean.height,
        path=str(path),
        user_id=user_id,
    )
    s.add(row)
    s.commit()
    return row
