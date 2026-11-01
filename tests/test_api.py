"""API integration tests: login, sessions, CSRF, TOTP, uploads, runs, questions, schedules,
profile/policy, and key scopes. Postgres-backed; no network, no model calls."""

from __future__ import annotations

import io
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from appa import db
from appa.api import app as api
from appa.api import auth, deps, users
from appa.api.routes import auth as auth_routes
from appa.api.routes import uploads as upload_routes
from appa.config import Candidate, Config, Rules

PASSWORD = secrets.token_urlsafe(18)  # generated per test run; no credentials in the repo


@pytest.fixture()
def client(factory, tmp_path, monkeypatch):
    for lim in (
        deps.key_limiter,
        deps.user_limiter,
        deps.fail_limiter,
        auth_routes.login_limiter,
        upload_routes.upload_limiter,
    ):
        lim.hits.clear()
    monkeypatch.setenv("APPA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        "appa.config.load",
        lambda path=None: Config(candidate=Candidate(name="t", application_email="t@x.edu"), rules=Rules()),
    )
    with factory() as s:
        users.create_user(s, "me", PASSWORD)
    return TestClient(api.app, base_url="https://testserver")


def login(c: TestClient, totp: str | None = None) -> dict:
    r = c.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD, **({"totp": totp} if totp else {})})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def png(w=40, h=30, **save) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format=save.pop("format", "PNG"), **save)
    return buf.getvalue()


# ---- login & sessions -------------------------------------------------------------


def test_login_sets_a_locked_down_cookie(client):
    r = client.post("/api/v1/auth/login", json={"username": "ME", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["username"] == "me"
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/api"):
        assert flag in cookie
    assert client.get("/api/v1/auth/me").json()["username"] == "me"


def test_bad_logins_look_identical(client):
    a = client.post("/api/v1/auth/login", json={"username": "me", "password": "wrong password!!"})
    b = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong password!!"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_login_rate_limit_and_lockout(client, factory):
    codes = [
        client.post("/api/v1/auth/login", json={"username": "me", "password": "nope nope nope"}).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5 and codes[5] == 429  # 5 per minute per IP
    auth_routes.login_limiter.hits.clear()
    # 5 failures locked the account: even the right password is refused, with the same message
    r = client.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD})
    assert r.status_code == 401
    with factory() as s:
        u = s.query(db.User).filter_by(username="me").one()
        assert u.locked_until is not None
        u.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert client.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD}).status_code == 200


def test_totp_required_once_enabled(client, factory):
    with factory() as s:
        u = s.query(db.User).filter_by(username="me").one()
        users.enable_totp(s, u)
        secret = u.totp_secret
    assert client.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD}).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"username": "me", "password": PASSWORD, "totp": "000000"}).status_code
        == 401
    )
    login(client, pyotp.TOTP(secret).now())


def test_logout_and_expiry(client, factory):
    h = login(client)
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    login(client)
    with factory() as s:
        sess = s.query(db.WebSession).one()
        sess.last_seen_at = datetime.now(UTC) - users.SESSION_IDLE - timedelta(minutes=1)
        s.commit()
    assert client.get("/api/v1/auth/me").status_code == 401  # idle timeout


def test_password_change_logs_out_everywhere(client, factory):
    login(client)
    with factory() as s:
        users.set_password(s, s.query(db.User).filter_by(username="me").one(), "a brand new long password")
    assert client.get("/api/v1/auth/me").status_code == 401


# ---- CSRF ---------------------------------------------------------------------------


def test_csrf_required_for_cookie_writes(client):
    h = login(client)
    body = {"role": "concierge"}
    assert client.post("/api/v1/runs", json=body).status_code == 403
    assert client.post("/api/v1/runs", json=body, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.post("/api/v1/runs", json=body, headers=h).status_code == 201
    assert client.get("/api/v1/runs").status_code == 200  # reads don't need it


def test_api_keys_skip_csrf_but_respect_scopes(client, factory):
    with factory() as s:
        runner = auth.create_key(s, "bot", ["runs:read", "runs:write"])
        apps_only = auth.create_key(s, "tracker", ["applications:read"])
    bot = {"Authorization": f"Bearer {runner}"}
    assert client.post("/api/v1/runs", json={"role": "concierge"}, headers=bot).status_code == 201
    assert client.get("/api/v1/runs", headers={"Authorization": f"Bearer {apps_only}"}).status_code == 403
    # Keys never reach human-only routes, whatever their scopes
    assert client.get("/api/v1/profile", headers=bot).status_code == 403
    assert client.post("/api/v1/uploads", headers=bot, files={"file": ("a.png", png(), "image/png")}).status_code == 403


# ---- uploads ------------------------------------------------------------------------


def test_upload_reencodes_and_strips_metadata(client, factory):
    h = login(client)
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    jpeg = png(format="JPEG", exif=exif.tobytes())
    r = client.post("/api/v1/uploads", headers=h, files={"file": ("shot.jpg", jpeg, "image/jpeg")})
    assert r.status_code == 201, r.text
    assert r.json()["mime"] == "image/png" and r.json()["width"] == 40
    with factory() as s:
        row = s.get(db.Upload, r.json()["id"])
    data = open(row.path, "rb").read()
    assert data.startswith(b"\x89PNG") and b"SecretCameraMaker" not in data
    import os
    import stat

    assert stat.S_IMODE(os.stat(row.path).st_mode) == 0o600


@pytest.mark.parametrize(
    "name,content,ctype",
    [
        ("evil.png", b"<html><script>alert(1)</script></html>", "image/png"),  # lies about its type
        ("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/svg+xml"),
        ("x.gif", None, "image/gif"),
        ("empty.png", b"", "image/png"),
    ],
)
def test_upload_rejects_non_images(client, name, content, ctype):
    h = login(client)
    if content is None:
        content = png(format="GIF")
    r = client.post("/api/v1/uploads", headers=h, files={"file": (name, content, ctype)})
    assert r.status_code == 422


def test_upload_rejects_huge_dimensions_and_size(client):
    h = login(client)
    r = client.post("/api/v1/uploads", headers=h, files={"file": ("big.png", png(9000, 10), "image/png")})
    assert r.status_code == 422 and "pixels" in r.json()["error"]
    r = client.post(
        "/api/v1/uploads", headers=h, files={"file": ("huge.png", b"\x89PNG" + b"0" * (11 * 1024 * 1024), "image/png")}
    )
    assert r.status_code == 413


def test_upload_needs_login(client):
    assert client.post("/api/v1/uploads", files={"file": ("a.png", png(), "image/png")}).status_code == 401


# ---- runs ---------------------------------------------------------------------------


def test_run_lifecycle_and_stream(client, factory):
    from appa.runtime import runs

    h = login(client)
    r = client.post("/api/v1/runs", headers=h, json={"role": "concierge", "budget": {"max_usd": 0.01}})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["created_by"] == "user:me" and r.json()["status"] == "queued"
    runs.execute(factory, rid)  # what a worker would do
    got = client.get(f"/api/v1/runs/{rid}").json()
    assert got["status"] == "succeeded"
    evs = client.get(f"/api/v1/runs/{rid}/events").json()["events"]
    assert [e["kind"] for e in evs][0] == "status" and evs[-1]["payload"]["status"] == "succeeded"
    stream = client.get(f"/api/v1/runs/{rid}/stream")
    assert stream.headers["content-type"].startswith("text/event-stream")
    body = stream.text
    assert body.count("event: status") >= 2 and "event: end" in body
    resumed = client.get(f"/api/v1/runs/{rid}/stream", headers={"Last-Event-ID": str(evs[-1]["id"])}).text
    assert "event: status" not in resumed and "event: end" in resumed


def test_run_input_rules(client, factory):
    h = login(client)
    bad = [
        ({"role": "scout", "input": {"screenshots": ["/etc/passwd"]}}, "upload_ids"),  # no server paths
        ({"role": "scout", "input": {"upload_ids": [999]}}, "unknown upload"),
        ({"role": "scout", "input": {"boards": ["myspace:foo"]}}, "invalid input"),
        ({"role": "scout", "model": "openai/not-allowed"}, "isn't in config/models.yaml"),
        ({"role": "nope"}, "unknown role"),
    ]
    for body, msg in bad:
        r = client.post("/api/v1/runs", headers=h, json=body)
        assert r.status_code == 422 and msg in r.json()["error"], (body, r.text)
    r = client.post("/api/v1/runs", headers=h, json={"role": "concierge", "budget": {"max_usd": 1000}})
    assert r.status_code == 422  # budget ceiling
    up = client.post("/api/v1/uploads", headers=h, files={"file": ("a.png", png(), "image/png")}).json()
    r = client.post("/api/v1/runs", headers=h, json={"role": "scout", "input": {"upload_ids": [up["id"]]}})
    assert r.status_code == 201 and r.json()["input"]["upload_ids"] == [up["id"]]


def test_cancel_run(client):
    h = login(client)
    rid = client.post("/api/v1/runs", headers=h, json={"role": "concierge"}).json()["id"]
    assert client.post(f"/api/v1/runs/{rid}/cancel", headers=h).json()["status"] == "cancelled"


# ---- questions, schedules, profile/policy -------------------------------------------


def test_answering_a_review_question_updates_the_application(client, factory):
    h = login(client)
    with factory() as s:
        app = db.Application(company="Hooli", role="SWE", status="Needs Review")
        s.add(app)
        s.flush()
        q = db.Question(kind="review", prompt="Review Hooli / SWE", application_id=app.id)
        s.add(q)
        s.commit()
        qid, aid = q.id, app.id
    open_qs = client.get("/api/v1/questions").json()["questions"]
    assert [x["id"] for x in open_qs] == [qid]
    r = client.post(f"/api/v1/questions/{qid}/answer", headers=h, json={"action": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "answered"
    with factory() as s:
        assert s.get(db.Application, aid).status == "To Apply"
    assert client.post(f"/api/v1/questions/{qid}/answer", headers=h, json={"action": "skip"}).status_code == 409


def test_choose_only_offered_postings(client, factory):
    h = login(client)
    with factory() as s:
        q = db.Question(
            kind="answer_needed",
            prompt="Which posting?",
            context={"options": [{"url": "https://job-boards.greenhouse.io/hooli/jobs/1"}]},
        )
        s.add(q)
        s.commit()
        qid = q.id
    r = client.post(
        f"/api/v1/questions/{qid}/answer", headers=h, json={"action": "choose", "choice": "https://evil.example/x"}
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/v1/questions/{qid}/answer",
        headers=h,
        json={"action": "choose", "choice": "https://job-boards.greenhouse.io/hooli/jobs/1"},
    )
    assert r.status_code == 200 and r.json()["run_id"]


def test_schedules_crud(client):
    h = login(client)
    r = client.post(
        "/api/v1/schedules", headers=h, json={"role": "scout", "cron": "0 */4 * * *", "input": {"boards": ["config"]}}
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.patch(f"/api/v1/schedules/{sid}", headers=h, json={"enabled": False}).json()["enabled"] is False
    assert (
        client.post("/api/v1/schedules", headers=h, json={"role": "scout", "cron": "every tuesday"}).status_code == 422
    )
    assert client.delete(f"/api/v1/schedules/{sid}", headers=h).status_code == 200
    assert client.get("/api/v1/schedules").json()["schedules"] == []


def test_profile_and_policy_are_versioned(client, factory):
    from appa import profile_store

    h = login(client)
    assert client.put("/api/v1/profile", headers=h, json={"text": "## Apply myself\n- Hooli"}).status_code == 200
    client.put("/api/v1/profile", headers=h, json={"text": "## Apply myself\n- Hooli\n- Globex"})
    got = client.get("/api/v1/profile").json()
    assert got["versions"] == 2 and "Globex" in got["text"]
    pol = client.get("/api/v1/policy").json()["policy"]
    pol["rules"]["max_years_experience"] = 1
    assert client.put("/api/v1/policy", headers=h, json=pol).status_code == 200
    pol["rules"]["same_company"] = "sometimes"
    assert client.put("/api/v1/policy", headers=h, json=pol).status_code == 422
    with factory() as s:
        assert profile_store.get_policy(s).rules.max_years_experience == 1  # what the agents now use


def test_meta_routes(client):
    login(client)
    assert {r["name"] for r in client.get("/api/v1/roles").json()["roles"]} == {"scout", "screener", "concierge"}
    m = client.get("/api/v1/models").json()
    assert m["defaults"]["screener"] in {x["name"] for x in m["models"]}


def test_security_headers_everywhere(client):
    r = client.get("/api/v1/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["referrer-policy"] == "no-referrer"
    r = client.get("/api/v1/runs")  # also on errors
    assert r.status_code == 401 and r.headers["x-content-type-options"] == "nosniff"


def test_system_status_and_kill_switch(client, factory):
    h = login(client)
    st = client.get("/api/v1/system").json()
    assert st["paused"] is False and "daily_cap_usd" in st
    client.post("/api/v1/runs", headers=h, json={"role": "concierge"})
    r = client.post("/api/v1/system/stop-all", headers=h).json()
    assert r == {"paused": True, "cancelled": 1}
    assert client.get("/api/v1/system").json()["paused"] is True
    assert client.post("/api/v1/system/resume", headers=h).json()["paused"] is False
    assert client.post("/api/v1/system/stop-all").status_code == 403  # CSRF still applies
