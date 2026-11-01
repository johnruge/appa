"""Human login: argon2id passwords, optional TOTP, lockout, and browser sessions.

A session cookie holds a random 256-bit token; the database stores only its SHA-256.
Each session also has a CSRF token the UI must echo in X-CSRF-Token on writes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from appa import db

COOKIE = "appa_session"
SESSION_TTL = timedelta(days=7)  # absolute lifetime
SESSION_IDLE = timedelta(hours=12)  # expires if unused this long
MAX_FAILURES = 5  # then lock the account...
LOCKOUT = timedelta(minutes=15)  # ...for this long
MIN_PASSWORD = 12

_ph = PasswordHasher()  # argon2id with library-recommended parameters
_DUMMY_HASH = _ph.hash(secrets.token_hex(16))  # equalizes timing for unknown usernames


def _now() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


def _sha(token: str) -> str:
    """SHA-256 hex digest of a session token."""
    return hashlib.sha256(token.encode()).hexdigest()


class LoginError(Exception):
    """Always the same message to the client: no hint whether the user exists."""


class Locked(LoginError):
    pass


def create_user(s: Session, username: str, password: str) -> db.User:
    """Create and commit a user after checking password length and username characters."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if not username.isascii() or not username.replace("_", "").replace("-", "").isalnum() or len(username) > 64:
        raise ValueError("username: letters, digits, - and _ only (max 64)")
    user = db.User(username=username.lower(), password_hash=_ph.hash(password))
    s.add(user)
    s.commit()
    return user


def set_password(s: Session, user: db.User, password: str) -> None:
    """Set a new password (length-checked) and log the user out of every session; commits."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    user.password_hash = _ph.hash(password)
    s.execute(delete(db.WebSession).where(db.WebSession.user_id == user.id))  # log out everywhere
    s.commit()


def enable_totp(s: Session, user: db.User) -> str:
    """Give the user a new TOTP secret, commit, and return its provisioning URI for an authenticator app."""
    user.totp_secret = pyotp.random_base32()
    s.commit()
    return pyotp.TOTP(user.totp_secret).provisioning_uri(name=user.username, issuer_name="appa")


def authenticate(s: Session, username: str, password: str, totp: str | None) -> db.User:
    """Check username, password and TOTP; counts failures, locks the account after too many, and commits."""
    user = s.scalars(select(db.User).where(db.User.username == username.lower()[:64])).first()
    if user is None:
        try:
            _ph.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        raise LoginError()
    if user.locked_until and user.locked_until > _now():
        raise Locked()
    ok = True
    try:
        _ph.verify(user.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    if ok and user.totp_secret:
        ok = bool(totp) and pyotp.TOTP(user.totp_secret).verify(totp, valid_window=1)
    if not ok:
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILURES:
            user.locked_until, user.failed_logins = _now() + LOCKOUT, 0
        s.commit()
        raise LoginError()
    if _ph.check_needs_rehash(user.password_hash):
        user.password_hash = _ph.hash(password)
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, _now()
    s.commit()
    return user


def start_session(s: Session, user: db.User, user_agent: str | None) -> tuple[str, db.WebSession]:
    """Create and commit a web session; returns the plaintext cookie token and the session row."""
    token = secrets.token_urlsafe(32)
    sess = db.WebSession(
        token_hash=_sha(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=_now() + SESSION_TTL,
        user_agent=(user_agent or "")[:300],
    )
    s.add(sess)
    s.commit()
    return token, sess


def load_session(s: Session, token: str) -> tuple[db.WebSession, db.User] | None:
    """The session and user for a cookie token, or None; deletes expired or idle sessions and refreshes last_seen."""
    if not token or len(token) > 100:
        return None
    sess = s.scalars(select(db.WebSession).where(db.WebSession.token_hash == _sha(token))).first()
    if not sess or not hmac.compare_digest(sess.token_hash, _sha(token)):
        return None
    now = _now()
    if sess.expires_at <= now or sess.last_seen_at + SESSION_IDLE <= now:
        s.delete(sess)
        s.commit()
        return None
    if now - sess.last_seen_at > timedelta(minutes=5):  # don't write on every request
        sess.last_seen_at = now
        s.commit()
    return sess, s.get(db.User, sess.user_id)


def end_session(s: Session, token: str) -> None:
    """Delete the session for this token and commit."""
    s.execute(delete(db.WebSession).where(db.WebSession.token_hash == _sha(token)))
    s.commit()


def csrf_ok(sess: db.WebSession, header: str | None) -> bool:
    """True if the header matches the session's CSRF token (constant-time compare)."""
    return bool(header) and hmac.compare_digest(sess.csrf_token, header)
