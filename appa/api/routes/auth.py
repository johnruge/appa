"""Login, logout, and who-am-I for the web UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from appa.api import auth, users
from appa.api.deps import SessionDep, UserDep, client_ip

router = APIRouter(prefix="/auth", tags=["auth"])
login_limiter = auth.RateLimiter(limit=5)  # login attempts per client IP per minute


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(None, pattern=r"^\d{6}$")


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, s: SessionDep):
    """Log in (rate-limited per IP) and set the session cookie; returns the CSRF token."""
    if not login_limiter.allow(client_ip(request)):
        raise HTTPException(429, "too many login attempts; wait a minute", headers={"Retry-After": "60"})
    try:
        user = users.authenticate(s, body.username, body.password, body.totp)
    except users.LoginError:
        # Same answer for unknown user, wrong password, wrong/missing code, or locked account.
        raise HTTPException(401, "invalid username, password or code") from None
    token, sess = users.start_session(s, user, request.headers.get("user-agent"))
    response.set_cookie(
        users.COOKIE,
        token,
        max_age=int(users.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/api",
    )
    return {"username": user.username, "csrf_token": sess.csrf_token, "totp_enabled": bool(user.totp_secret)}


@router.post("/logout")
def logout(p: UserDep, request: Request, response: Response, s: SessionDep):
    """Log out: end the session and clear the cookie."""
    users.end_session(s, request.cookies.get(users.COOKIE, ""))
    response.delete_cookie(users.COOKIE, path="/api", secure=True, httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/me")
def me(p: UserDep):
    """Return the logged-in user, their CSRF token, and whether TOTP is on."""
    return {
        "username": p.user.username,
        "csrf_token": p.web_session.csrf_token,
        "totp_enabled": bool(p.user.totp_secret),
    }
