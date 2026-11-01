"""Who is calling, and what may they do.

Two kinds of callers:
- You, in the browser: session cookie (HttpOnly, Secure, SameSite=Strict) + X-CSRF-Token
  header on every write. Humans have every scope.
- Agents and scripts: `Authorization: Bearer appa_...` API key with explicit scopes.
  No cookies, so no CSRF; keys can never reach human-only routes (profile, uploads...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from appa import db
from appa.api import auth, users

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
key_limiter = auth.RateLimiter(limit=120)  # requests per API key per minute
user_limiter = auth.RateLimiter(limit=300)  # requests per browser session per minute
fail_limiter = auth.RateLimiter(limit=10)  # failed auth attempts per client IP per minute


def get_session():
    """FastAPI dependency yielding one database session per request."""
    with db.sessions()() as s:
        yield s


SessionDep = Annotated[Session, Depends(get_session)]


@dataclass
class Principal:
    kind: str  # "user" | "key"
    name: str  # username or key name
    scopes: set[str] = field(default_factory=set)
    user: db.User | None = None
    web_session: db.WebSession | None = None

    @property
    def label(self) -> str:  # recorded as created_by on runs / applications
        """'user:<name>' or 'api:<name>', recorded as created_by on runs and applications."""
        return f"{'user' if self.kind == 'user' else 'api'}:{self.name}"


def client_ip(request: Request) -> str:
    """The client's IP address, or 'unknown'."""
    return request.client.host if request.client else "unknown"


def _deny(request: Request, status: int, message: str, headers: dict | None = None):
    """Raise the given HTTP error; failed logins over the per-IP limit get a 429 instead of a 401."""
    if status == 401 and not fail_limiter.allow(client_ip(request)):
        raise HTTPException(429, "too many failed attempts", headers={"Retry-After": "60"})
    raise HTTPException(status, message, headers=headers)


def current(request: Request, s: SessionDep) -> Principal:
    """Authenticate a request by bearer API key or session cookie (with CSRF check on writes), rate-limited per
    caller."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        key = auth.verify(s, header[7:].strip())
        if not key:
            _deny(request, 401, "missing or invalid credentials", {"WWW-Authenticate": "Bearer"})
        if not key_limiter.allow(f"key:{key.prefix}"):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        return Principal("key", key.name, set(key.scopes))
    loaded = users.load_session(s, request.cookies.get(users.COOKIE, ""))
    if not loaded:
        _deny(request, 401, "missing or invalid credentials", {"WWW-Authenticate": "Bearer"})
    sess, user = loaded
    if request.method in UNSAFE and not users.csrf_ok(sess, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "missing or invalid CSRF token")
    if not user_limiter.allow(f"user:{user.id}"):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
    return Principal("user", user.username, set(auth.SCOPES) - {"runner"}, user=user, web_session=sess)


CurrentDep = Annotated[Principal, Depends(current)]


def require(scope: str):
    """A dependency that lets the request through only if the caller has the given scope (else 403)."""

    def dep(p: CurrentDep) -> Principal:
        """Return the caller if it has the scope, else raise 403."""
        if scope not in p.scopes:
            raise HTTPException(403, f"not allowed: needs scope {scope}")
        return p

    return dep


def require_user(p: CurrentDep) -> Principal:
    """Allow only logged-in users, not API keys (else 403)."""
    if p.kind != "user":
        raise HTTPException(403, "this route is for logged-in users, not API keys")
    return p


UserDep = Annotated[Principal, Depends(require_user)]


def require_runner(p: CurrentDep) -> Principal:
    """Only a key with the runner scope; humans and other keys can't drive the runner endpoints."""
    if p.kind != "key" or "runner" not in p.scopes:
        raise HTTPException(403, "runner endpoints need a key with the runner scope")
    return p


RunnerDep = Annotated[Principal, Depends(require_runner)]
