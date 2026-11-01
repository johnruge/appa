"""API keys for agents, and a small in-process rate limiter.

Tokens look like `appa_<prefix>_<secret>` and are shown once at creation.
Only their SHA-256 hash is stored, so a copied database file doesn't leak usable keys.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from appa.db import ApiKey

SCOPES = {"applications:read", "applications:write", "runs:read", "runs:write", "runner"}


def _hash(token: str) -> str:
    """SHA-256 hex digest of a token (only the hash is stored)."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_key(s: Session, name: str, scopes: list[str]) -> str:
    """Create and commit a new API key with the given scopes; returns the plaintext token, shown only once."""
    bad = set(scopes) - SCOPES
    if bad:
        raise ValueError(f"unknown scopes {sorted(bad)}; valid: {sorted(SCOPES)}")
    prefix = secrets.token_hex(4)
    token = f"appa_{prefix}_{secrets.token_urlsafe(32)}"
    s.add(ApiKey(name=name, prefix=prefix, token_hash=_hash(token), scopes=sorted(scopes)))
    s.commit()
    return token


def verify(s: Session, token: str) -> ApiKey | None:
    """The live key matching a token, or None; stamps last_used_at and commits."""
    if not token.startswith("appa_") or len(token) > 100:
        return None
    key = s.scalars(select(ApiKey).where(ApiKey.token_hash == _hash(token))).first()
    if not key or key.revoked_at or not hmac.compare_digest(key.token_hash, _hash(token)):
        return None
    key.last_used_at = datetime.now(UTC)
    s.commit()
    return key


def revoke(s: Session, name: str) -> bool:
    """Revoke the active key with this name and commit; False if there was none."""
    key = s.scalars(select(ApiKey).where(ApiKey.name == name, ApiKey.revoked_at.is_(None))).first()
    if not key:
        return False
    key.revoked_at = datetime.now(UTC)
    s.commit()
    return True


class RateLimiter:
    """Sliding window: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0):
        """A sliding-window limiter allowing `limit` hits per key per `window` seconds."""
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record a hit for the key and return True, or return False if it is over the limit."""
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True
