from __future__ import annotations

from dataclasses import dataclass
import asyncio
import copy
import json
import time
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
            "check the configured ANTHROPIC_API_KEY or your client-supplied key.",
        },
    }

from .config import Settings
from .sse import SSEUsageTracker
from .yscompress import estimate_tokens


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


def maybe_apply_prompt_cache(body: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "off":
        return body
    if mode == "passthrough" and "cache_control" not in body:
        return body
    out = copy.deepcopy(body)
    out.setdefault("cache_control", {"type": "ephemeral"})
    return out


def clean_gateway_fields(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in {"decompute", "yellowstone"}}


def upstream_headers(settings: Settings, upstream_api_key: str | None, incoming_headers: dict[str, str] | None = None, *, stream: bool = False) -> dict[str, str]:
    headers = {
        "anthropic-version": settings.anthropic_version,
        "content-type": "application/json",
    }
    if upstream_api_key:
        headers["x-api-key"] = upstream_api_key
    if stream:
        headers["accept"] = "text/event-stream"
    incoming_headers = incoming_headers or {}
    beta = incoming_headers.get("anthropic-beta")
    if beta:
        allowed = [b.strip() for b in beta.split(",") if b.strip() in settings.allowed_anthropic_beta_headers]
        if allowed:
            headers["anthropic-beta"] = ",".join(allowed)
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


async def call_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: str | None,
    prompt_cache_mode: str,
    extra_headers: dict[str, str] | None = None,
) -> ProviderResult:
    if settings.mock_anthropic or not upstream_api_key:
        return mock_claude_response(body)
    clean_body = maybe_apply_prompt_cache(clean_gateway_fields(body), prompt_cache_mode)
    headers = upstream_headers(settings, upstream_api_key, extra_headers)
    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    async with httpx.AsyncClient(timeout=None) as client:
        for attempt in range(settings.upstream_max_retries + 1):
            resp = await client.post(url, json=clean_body, headers=headers)
            if resp.status_code not in RETRYABLE_STATUS or attempt >= settings.upstream_max_retries:
                break
            await asyncio.sleep(settings.upstream_retry_backoff_s * (2 ** attempt))
    if resp.status_code in (401, 403):
        # Hide provider key/account detail; keep headers (request-id) for tracing.
        return ProviderResult(status_code=resp.status_code, body=_sanitize_auth_error(resp.status_code), headers=dict(resp.headers))
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def list_anthropic_models(*, settings: Settings, upstream_api_key: str | None, extra_headers: dict[str, str] | None = None) -> ProviderResult:
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
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def count_tokens(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: str | None,
    extra_headers: dict[str, str] | None = None,
) -> ProviderResult:
    if settings.mock_anthropic or not upstream_api_key:
        tokens = estimate_tokens(body.get("messages")) + estimate_tokens(body.get("system")) + estimate_tokens(body.get("tools"))
        return ProviderResult(status_code=200, body={"input_tokens": tokens, "source": "local_approximation"}, headers={"request-id": "req_mock"})
    headers = upstream_headers(settings, upstream_api_key, extra_headers)
    clean_body = clean_gateway_fields(body)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(settings.anthropic_base_url.rstrip("/") + "/v1/messages/count_tokens", json=clean_body, headers=headers)
    try:
        payload = resp.json()
    except Exception:
        payload = {"type": "error", "error": {"message": resp.text}}
    return ProviderResult(status_code=resp.status_code, body=payload, headers=dict(resp.headers))


async def stream_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: str | None,
    prompt_cache_mode: str,
    tracker: SSEUsageTracker,
    extra_headers: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    if settings.mock_anthropic or not upstream_api_key:
        tracker.upstream_request_id = "req_mock"
        payload = mock_claude_response(body).body
        text = payload["content"][0]["text"]
        chunks = [
            f"event: message_start\ndata: {json_dumps({k:v for k,v in payload.items() if k != 'content'})}\n\n".encode(),
            f"event: content_block_start\ndata: {json_dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n".encode(),
            f"event: content_block_delta\ndata: {json_dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n".encode(),
            f"event: content_block_stop\ndata: {json_dumps({'type':'content_block_stop','index':0})}\n\n".encode(),
            f"event: message_delta\ndata: {json_dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':payload['usage']})}\n\n".encode(),
            f"event: message_stop\ndata: {json_dumps({'type':'message_stop'})}\n\n".encode(),
        ]
        for c in chunks:
            tracker.feed(c)
            yield c
        return

    clean_body = maybe_apply_prompt_cache(clean_gateway_fields(body), prompt_cache_mode)
    headers = upstream_headers(settings, upstream_api_key, extra_headers, stream=True)
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", settings.anthropic_base_url.rstrip("/") + "/v1/messages", json=clean_body, headers=headers) as resp:
            tracker.upstream_request_id = resp.headers.get("request-id") or resp.headers.get("anthropic-request-id") or resp.headers.get("x-request-id")
            if resp.status_code >= 400:
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
                return
            async for chunk in resp.aiter_bytes():
                tracker.feed(chunk)
                yield chunk
