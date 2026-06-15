from __future__ import annotations

from dataclasses import dataclass
import asyncio
import copy
import json
import random
import time
from contextlib import suppress
from typing import Any, AsyncIterator

import httpx

# Transient upstream failures worth a backoff retry (per Anthropic error docs).
RETRYABLE_STATUS = {500, 504, 529}


def _sanitize_auth_error(status_code: int) -> dict[str, Any]:
    """Never forward the provider's raw 401/403 body — it can echo key material or
    account detail. Return a clean, actionable error instead. The upstream
    request-id stays in the response headers so the incident is still traceable."""
    return {
        "type": "error",
        "error": {
            "type": "authentication_error" if status_code == 401 else "permission_error",
            "message": f"Upstream provider rejected the credentials (HTTP {status_code}). "
            "The provider's error detail is withheld by the Decompute gateway; "
            "check the configured Anthropic credentials or your client-supplied credentials.",
        },
    }

from .config import Settings
from .security import UpstreamAnthropicAuth
from .sse import SSEUsageTracker
from .yscompress import estimate_tokens


def _stream_timeout(settings: Settings) -> httpx.Timeout:
    """Timeout for a streaming upstream call. `read` bounds the GAP BETWEEN chunks (not the
    whole response) — Anthropic emits periodic pings, so a real stream stays well under it,
    while a stalled upstream is broken instead of hanging the proxy forever. No overall cap,
    so a legitimately long generation is never cut off mid-stream."""
    return httpx.Timeout(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_read_timeout_s,
        write=settings.upstream_write_timeout_s,
        pool=settings.upstream_connect_timeout_s,
    )


def _nonstream_timeout(settings: Settings) -> httpx.Timeout:
    """Timeout for a unary upstream call. The full body lands at once and can take minutes,
    so `read` is the generous overall budget rather than a between-chunk gap."""
    return httpx.Timeout(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_overall_timeout_s,
        write=settings.upstream_write_timeout_s,
        pool=settings.upstream_connect_timeout_s,
    )


def _retry_backoff_s(settings: Settings, attempt: int) -> float:
    """Exponential backoff with jitter (full-jitter, capped) so concurrent Claude Code
    requests retrying a 529 don't resynchronize into a thundering herd."""
    base = settings.upstream_retry_backoff_s * (2 ** attempt)
    return base + random.uniform(0.0, base * 0.25)


@dataclass
class ProviderResult:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]

    @property
    def upstream_request_id(self) -> str | None:
        return self.headers.get("request-id") or self.headers.get("anthropic-request-id") or self.headers.get("x-request-id")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request_has_cache_control(body: dict[str, Any]) -> bool:
    """True if the client already manages prompt caching anywhere (top-level, system blocks,
    tools, or message content blocks). Claude Code sets cache_control (incl. ttl='1h') itself."""
    if "cache_control" in body:
        return True
    sysv = body.get("system")
    if isinstance(sysv, list) and any(isinstance(b, dict) and b.get("cache_control") for b in sysv):
        return True
    for t in (body.get("tools") or []):
        if isinstance(t, dict) and t.get("cache_control"):
            return True
    for m in (body.get("messages") or []):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("cache_control") for b in c):
            return True
    return False


def maybe_apply_prompt_cache(body: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "off":
        return body
    # Never override the client's caching. Claude Code marks blocks with cache_control (e.g.
    # ttl='1h'); adding a top-level cache_control (ttl 5m) makes Anthropic 400 with
    # "ttl='5m' but the target block already has cache_control with ttl='1h'". When the client
    # already manages caching anywhere, pass through untouched.
    if _request_has_cache_control(body):
        return body
    if mode == "passthrough":
        return body
    out = copy.deepcopy(body)
    out.setdefault("cache_control", {"type": "ephemeral"})
    return out


def clean_gateway_fields(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in {"decompute", "yellowstone"}}


def _apply_upstream_auth(headers: dict[str, str], upstream_auth: UpstreamAnthropicAuth | str | None) -> None:
    if isinstance(upstream_auth, UpstreamAnthropicAuth):
        if upstream_auth.kind == "bearer":
            headers["authorization"] = f"Bearer {upstream_auth.value}"
        elif upstream_auth.kind == "api_key":
            headers["x-api-key"] = upstream_auth.value
        return
    if upstream_auth:
        headers["x-api-key"] = upstream_auth


def upstream_headers(settings: Settings, upstream_api_key: UpstreamAnthropicAuth | str | None, incoming_headers: dict[str, str] | None = None, *, stream: bool = False) -> dict[str, str]:
    headers = {
        "anthropic-version": settings.anthropic_version,
        "content-type": "application/json",
    }
    _apply_upstream_auth(headers, upstream_api_key)
    if stream:
        headers["accept"] = "text/event-stream"
    incoming_headers = incoming_headers or {}
    beta = incoming_headers.get("anthropic-beta")
    if beta:
        # Transparent-proxy default: forward the client's anthropic-beta header verbatim.
        # These betas enable request fields the client also sends (context_management, 1M
        # context, fine-grained tool streaming, interleaved thinking, prompt-caching); if we
        # drop the header, Anthropic rejects the body it gates (e.g. 400 "context_management:
        # Extra inputs are not permitted"). Set DECOMPUTE_ALLOWED_ANTHROPIC_BETAS to an explicit
        # allowlist to RESTRICT which betas pass through; empty (the default) = allow all.
        allowlist = settings.allowed_anthropic_beta_headers
        if allowlist:
            allowed = [b.strip() for b in beta.split(",") if b.strip() in allowlist]
            if allowed:
                headers["anthropic-beta"] = ",".join(allowed)
        else:
            headers["anthropic-beta"] = beta
    return headers


def mock_claude_response(body: dict[str, Any]) -> ProviderResult:
    model = body.get("model", "claude-mock")
    messages = body.get("messages") or []
    token_in = estimate_tokens(messages) + estimate_tokens(body.get("system"))
    text = "Mock Claude response from Decompute Gateway. Disable DECOMPUTE_MOCK_ANTHROPIC and configure ANTHROPIC_API_KEY for upstream calls."
    payload = {
        "id": "msg_mock_" + str(int(time.time() * 1000)),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": token_in, "output_tokens": estimate_tokens(text)},
    }
    return ProviderResult(status_code=200, body=payload, headers={"request-id": "req_mock"})


def mock_claude_stream_chunks(payload: dict[str, Any]) -> list[bytes]:
    """Anthropic-shaped SSE chunks for the local/mock provider path.

    Claude Code requires `message_start` to wrap the message object under a
    `message` key. Sending the raw message object makes the client ignore the
    start event, then fail on the next content_block_delta with "without a
    current message".
    """

    text = payload["content"][0]["text"]
    message = {k: v for k, v in payload.items() if k != "content"}
    message["content"] = []
    message["usage"] = {"input_tokens": payload["usage"]["input_tokens"], "output_tokens": 0}
    output_tokens = payload["usage"]["output_tokens"]
    return [
        f"event: message_start\ndata: {json_dumps({'type': 'message_start', 'message': message})}\n\n".encode(),
        f"event: content_block_start\ndata: {json_dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n".encode(),
        f"event: content_block_delta\ndata: {json_dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n".encode(),
        f"event: content_block_stop\ndata: {json_dumps({'type':'content_block_stop','index':0})}\n\n".encode(),
        f"event: message_delta\ndata: {json_dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':output_tokens}})}\n\n".encode(),
        f"event: message_stop\ndata: {json_dumps({'type':'message_stop'})}\n\n".encode(),
    ]


async def call_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: UpstreamAnthropicAuth | str | None,
    prompt_cache_mode: str,
    extra_headers: dict[str, str] | None = None,
) -> ProviderResult:
    if settings.mock_anthropic or not upstream_api_key:
        return mock_claude_response(body)
    clean_body = maybe_apply_prompt_cache(clean_gateway_fields(body), prompt_cache_mode)
    headers = upstream_headers(settings, upstream_api_key, extra_headers)
    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    async with httpx.AsyncClient(timeout=_nonstream_timeout(settings)) as client:
        for attempt in range(settings.upstream_max_retries + 1):
            resp = await client.post(url, json=clean_body, headers=headers)
            if resp.status_code not in RETRYABLE_STATUS or attempt >= settings.upstream_max_retries:
                break
            await asyncio.sleep(_retry_backoff_s(settings, attempt))
    if resp.status_code in (401, 403):
        # Hide provider key/account detail; keep headers (request-id) for tracing.
        return ProviderResult(status_code=resp.status_code, body=_sanitize_auth_error(resp.status_code), headers=dict(resp.headers))
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def list_anthropic_models(*, settings: Settings, upstream_api_key: UpstreamAnthropicAuth | str | None, extra_headers: dict[str, str] | None = None) -> ProviderResult:
    if settings.mock_anthropic or not upstream_api_key:
        data = [
            {"id": "claude-sonnet-4-5", "type": "model", "display_name": "Claude Sonnet", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "claude-opus-4-1", "type": "model", "display_name": "Claude Opus", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "claude-haiku-4-5", "type": "model", "display_name": "Claude Haiku", "created_at": "2026-01-01T00:00:00Z"},
        ]
        return ProviderResult(status_code=200, body={"data": data}, headers={"request-id": "req_mock"})
    headers = upstream_headers(settings, upstream_api_key, extra_headers)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.anthropic_base_url.rstrip("/") + "/v1/models", headers=headers)
    if resp.status_code in (401, 403):
        # Same credential-hygiene rule as call_anthropic_messages: never forward the provider's raw
        # 401/403 body (it can echo key material/account detail); keep headers (request-id) for tracing.
        return ProviderResult(status_code=resp.status_code, body=_sanitize_auth_error(resp.status_code), headers=dict(resp.headers))
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def count_tokens(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: UpstreamAnthropicAuth | str | None,
    extra_headers: dict[str, str] | None = None,
) -> ProviderResult:
    if settings.mock_anthropic or not upstream_api_key:
        tokens = estimate_tokens(body.get("messages")) + estimate_tokens(body.get("system")) + estimate_tokens(body.get("tools"))
        return ProviderResult(status_code=200, body={"input_tokens": tokens, "source": "local_approximation"}, headers={"request-id": "req_mock"})
    headers = upstream_headers(settings, upstream_api_key, extra_headers)
    clean_body = clean_gateway_fields(body)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.anthropic_base_url.rstrip("/") + "/v1/messages/count_tokens", json=clean_body, headers=headers)
    if resp.status_code in (401, 403):
        # Withhold the provider's raw auth-error body (key/account detail); keep request-id for tracing.
        return ProviderResult(status_code=resp.status_code, body=_sanitize_auth_error(resp.status_code), headers=dict(resp.headers))
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def stream_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: UpstreamAnthropicAuth | str | None,
    prompt_cache_mode: str,
    tracker: SSEUsageTracker,
    extra_headers: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    if settings.mock_anthropic or not upstream_api_key:
        tracker.upstream_request_id = "req_mock"
        payload = mock_claude_response(body).body
        for c in mock_claude_stream_chunks(payload):
            tracker.feed(c)
            yield c
        return

    clean_body = maybe_apply_prompt_cache(clean_gateway_fields(body), prompt_cache_mode)
    headers = upstream_headers(settings, upstream_api_key, extra_headers, stream=True)
    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    async with httpx.AsyncClient(timeout=_stream_timeout(settings)) as client:
        # Retry a transient upstream status (500/504/529) BEFORE any byte is yielded — once
        # we forward a chunk the HTTP 200 status line is committed and retrying is impossible.
        # This absorbs Anthropic overload at the gateway instead of surfacing it as an error
        # event that Claude Code would itself retry, turning one 529 into a client-side storm.
        for attempt in range(settings.upstream_max_retries + 1):
            async with client.stream("POST", url, json=clean_body, headers=headers) as resp:
                tracker.upstream_request_id = (
                    resp.headers.get("request-id")
                    or resp.headers.get("anthropic-request-id")
                    or resp.headers.get("x-request-id")
                )
                if resp.status_code in RETRYABLE_STATUS and attempt < settings.upstream_max_retries:
                    # Drain the small error body so the connection closes cleanly, then retry.
                    with suppress(Exception):
                        await resp.aread()
                else:
                    if resp.status_code >= 400:
                        if resp.status_code in (401, 403):
                            # Mirror the non-stream path: never forward the provider's raw 401/403
                            # body — it can echo key material/account detail, and this SSE error is
                            # also persisted in the usage record (stream_json). Drain + discard it.
                            with suppress(Exception):
                                await resp.aread()
                            err_json = _sanitize_auth_error(resp.status_code)
                        else:
                            try:
                                err_body = await resp.aread()
                                err_json = json.loads(err_body.decode("utf-8"))
                                if not isinstance(err_json, dict):
                                    err_json = {"type": "error", "error": {"message": str(err_json)}}
                            except Exception:
                                err_json = {"type": "error", "error": {"message": f"upstream HTTP {resp.status_code}"}}
                        # Carry the real upstream HTTP status on the SSE error payload so the
                        # gateway can record it (rather than collapsing every stream error to
                        # 502); SSE events otherwise have no place for an HTTP status.
                        err_json.setdefault("http_status", resp.status_code)
                        chunk = f"event: error\ndata: {json_dumps(err_json)}\n\n".encode()
                        tracker.feed(chunk)
                        yield chunk
                    else:
                        async for chunk in resp.aiter_bytes():
                            tracker.feed(chunk)
                            yield chunk
                    return
            # Reached only on the retry path (the response context has now closed cleanly).
            await asyncio.sleep(_retry_backoff_s(settings, attempt))
