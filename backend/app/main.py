"""FastAPI application entry point with fail-closed operational controls."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.operations import router as operations_router
from app.api.routes import router as api_router
from app.auth.routes import jwt_service
from app.auth.routes import router as auth_router
from app.db.runtime import create_control_plane_repositories
from app.observability import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    READINESS_FAILURES,
    configure_logging,
)
from app.runtime import runtime_settings, validate_startup_secrets


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MISSING_STATE = object()
_settings = runtime_settings()
configure_logging()
_log = structlog.get_logger("api")


class BodyLimitMiddleware:
    """Enforce a byte limit for both Content-Length and streamed request bodies."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await _send_too_large(send)
                return
        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestTooLarge:
            await _send_too_large(send)


class _RequestTooLarge(Exception):
    pass


async def _send_too_large(send: Send) -> None:
    body = b'{"detail":"Request body is too large."}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate secrets and attach the durable repository graph to this process."""
    settings = runtime_settings()
    validate_startup_secrets(settings)
    jwt_service()
    engine, repositories = create_control_plane_repositories()
    previous_engine = getattr(
        application.state, "control_plane_engine", _MISSING_STATE
    )
    previous_repositories = getattr(application.state, "control_plane", _MISSING_STATE)
    application.state.control_plane_engine = engine
    application.state.control_plane = repositories
    try:
        yield
    finally:
        await engine.dispose()
        if previous_engine is _MISSING_STATE:
            del application.state.control_plane_engine
        else:
            application.state.control_plane_engine = previous_engine
        if previous_repositories is _MISSING_STATE:
            del application.state.control_plane
        else:
            application.state.control_plane = previous_repositories


app = FastAPI(
    title="AI-Driven Autonomous NoSQL Query Optimization Framework",
    lifespan=lifespan,
)
app.include_router(auth_router)
app.include_router(api_router)
app.include_router(operations_router)
app.add_middleware(BodyLimitMiddleware, max_bytes=_settings.max_request_body_bytes)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_settings.allowed_hosts))
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach correlation/security headers and emit literal-free request metrics."""
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = supplied_id if _REQUEST_ID.fullmatch(supplied_id) else str(uuid4())
    started = time.monotonic()
    content_length = request.headers.get("content-length")
    if content_length is not None and (
        not content_length.isdigit() or int(content_length) > _settings.max_request_body_bytes
    ):
        response = JSONResponse(
            status_code=413, content={"detail": "Request body is too large."}
        )
    else:
        try:
            response = await call_next(request)
        except Exception:
            _log.exception("unhandled_request_error", request_id=request_id, service="api")
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error.", "request_id": request_id},
            )
    route = request.scope.get("route")
    route_label = getattr(route, "path", "unmatched")
    duration = time.monotonic() - started
    HTTP_REQUESTS.labels(request.method, route_label, str(response.status_code)).inc()
    HTTP_DURATION.labels(request.method, route_label).observe(duration)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "font-src 'self' data: https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    _log.info(
        "request_complete",
        request_id=request_id,
        service="api",
        method=request.method,
        route=route_label,
        status_code=response.status_code,
        duration_ms=round(duration * 1000, 3),
    )
    return response


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    """Report only that the process and event loop are alive."""
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready(request: Request) -> Response:
    """Report ready only when authoritative PostgreSQL is usable."""
    try:
        async with request.app.state.control_plane_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        READINESS_FAILURES.labels("api").inc()
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose bounded process metrics without application/query labels."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
