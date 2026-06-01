from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware


request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
project_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("project_id", default=None)

REQUESTS = Counter("decompute_requests_total", "Gateway requests", ["method", "path", "status"])
LATENCY = Histogram("decompute_request_latency_seconds", "Gateway request latency", ["method", "path"])
UPSTREAM = Counter("decompute_upstream_responses_total", "Upstream provider responses", ["provider", "status"])
COMPRESSION_TOKENS = Counter("decompute_tokens_total", "Token counters", ["kind"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "project_id": project_id_var.get(),
        }
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            payload.update(record.extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def current_request_id() -> str:
    rid = request_id_var.get()
    if not rid:
        rid = "req_" + uuid.uuid4().hex[:24]
        request_id_var.set(rid)
    return rid


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-decompute-request-id") or "req_" + uuid.uuid4().hex[:24]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = time.perf_counter() - start
            REQUESTS.labels(request.method, request.url.path, status).inc()
            LATENCY.labels(request.method, request.url.path).observe(elapsed)
            request_id_var.reset(token)


_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("x-decompute-request-id", current_request_id())
        # Skip restrictive security headers for Swagger UI paths — they load
        # external CDN assets and an inline init script that a strict CSP blocks.
        if request.url.path in _DOCS_PATHS:
            return response
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "no-referrer")
        response.headers.setdefault("permissions-policy", "geolocation=(), microphone=(), camera=()")
        if request.url.scheme == "https":
            response.headers.setdefault("strict-transport-security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("content-security-policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return Response("Request too large", status_code=413)
            except ValueError:
                pass
        return await call_next(request)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
