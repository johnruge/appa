"""appa HTTP API. Everything lives under /api/v1.

Public: GET /api/v1/health and POST /api/v1/auth/login (rate-limited). Everything else
needs a browser session (with CSRF token on writes) or a scoped API key; see deps.py.

Run locally with `appa serve` (127.0.0.1 only). Deployed, it sits behind Caddy, which
terminates TLS, enforces body limits and is the only public port.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from appa import uploads
from appa.api.routes import applications, apply, auth, feeds, meta, profile, questions, runner, runs, schedules, system
from appa.api.routes import uploads as upload_routes

log = logging.getLogger("appa.api")

PREFIX = "/api/v1"
MAX_BODY_BYTES = 64 * 1024
MAX_UPLOAD_BODY = uploads.MAX_BYTES + 64 * 1024  # file + multipart overhead
MAX_FORM_BODY = 2 * 1024 * 1024  # runner form reports (questions + their options)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
}

app = FastAPI(title="appa", docs_url=None, redoc_url=None, openapi_url=None)  # no route discovery

for r in (
    meta.router,
    auth.router,
    applications.router,
    runs.router,
    questions.router,
    schedules.router,
    profile.router,
    upload_routes.router,
    system.router,
    apply.router,
    runner.router,
    feeds.router,
):
    app.include_router(r, prefix=PREFIX)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Middleware: enforce per-route body size limits, require content-length on writes, and add security headers."""
    path = request.url.path
    is_upload = path == f"{PREFIX}/uploads" or (
        path.startswith(f"{PREFIX}/runner/attempts/") and path.endswith("/screenshot")
    )
    is_form = path.startswith(f"{PREFIX}/runner/attempts/") and path.endswith("/form")
    limit = MAX_UPLOAD_BODY if is_upload else MAX_FORM_BODY if is_form else MAX_BODY_BYTES
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > limit):
        return JSONResponse({"error": "request body too large"}, status_code=413, headers=SECURITY_HEADERS)
    if request.method in ("POST", "PATCH", "PUT") and not length:
        return JSONResponse({"error": "content-length required"}, status_code=411, headers=SECURITY_HEADERS)
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.exception_handler(RequestValidationError)
async def _validation(_req: Request, exc: RequestValidationError):
    """Return validation errors as a 422 listing each bad field and its message."""
    errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse({"error": "invalid request", "details": errors}, status_code=422)


@app.exception_handler(HTTPException)
async def _http(_req: Request, exc: HTTPException):
    """Return an HTTPException as {'error': detail} with its status and headers."""
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def _unhandled(_req: Request, exc: Exception):
    """Log an unhandled error server-side and return a bare 500 with no details."""
    log.exception("unhandled error")  # details go to the server log, never the client
    return JSONResponse({"error": "internal error"}, status_code=500)
