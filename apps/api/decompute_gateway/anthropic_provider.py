from __future__ import annotations

import copy
import time
from typing import Any

import httpx

from .config import Settings
from .yscompress import estimate_tokens


def maybe_apply_prompt_cache(body: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "off":
        return body
    if mode == "passthrough" and "cache_control" not in body:
        return body
    # Current Anthropic API supports a top-level cache_control option for automatic caching.
    out = copy.deepcopy(body)
    out.setdefault("cache_control", {"type": "ephemeral"})
    return out


async def call_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: str | None,
    prompt_cache_mode: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if settings.mock_anthropic or not upstream_api_key:
        return mock_claude_response(body)

    clean_body = {k: v for k, v in body.items() if k not in {"decompute", "yellowstone"}}
    clean_body = maybe_apply_prompt_cache(clean_body, prompt_cache_mode)
    headers = {
        "x-api-key": upstream_api_key,
        "anthropic-version": settings.anthropic_version,
        "content-type": "application/json",
    }
    if extra_headers:
        # Pass through Anthropic beta headers when present.
        for key in ("anthropic-beta",):
            if key in extra_headers:
                headers[key] = extra_headers[key]
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(settings.anthropic_base_url.rstrip("/") + "/v1/messages", json=clean_body, headers=headers)
    if resp.status_code >= 400:
        return {"type": "error", "error": {"status_code": resp.status_code, "message": resp.text}}
    return resp.json()


def mock_claude_response(body: dict[str, Any]) -> dict[str, Any]:
    model = body.get("model", "claude-mock")
    messages = body.get("messages") or []
    token_in = estimate_tokens(messages) + estimate_tokens(body.get("system"))
    text = (
        "Mock Claude response from Decompute Gateway. "
        "Set ANTHROPIC_API_KEY and DECOMPUTE_MOCK_ANTHROPIC=false to call Claude upstream."
    )
    return {
        "id": "msg_mock_" + str(int(time.time() * 1000)),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": token_in, "output_tokens": estimate_tokens(text)},
    }

async def stream_anthropic_messages(
    body: dict[str, Any],
    *,
    settings: Settings,
    upstream_api_key: str | None,
    prompt_cache_mode: str,
    extra_headers: dict[str, str] | None = None,
):
    if settings.mock_anthropic or not upstream_api_key:
        payload = mock_claude_response(body)
        text = payload["content"][0]["text"]
        yield f"event: message_start\ndata: {json_dumps({k:v for k,v in payload.items() if k != 'content'})}\n\n".encode()
        yield f"event: content_block_start\ndata: {json_dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n".encode()
        yield f"event: content_block_delta\ndata: {json_dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n".encode()
        yield f"event: content_block_stop\ndata: {json_dumps({'type':'content_block_stop','index':0})}\n\n".encode()
        yield f"event: message_delta\ndata: {json_dumps({'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':payload['usage']})}\n\n".encode()
        yield f"event: message_stop\ndata: {json_dumps({'type':'message_stop'})}\n\n".encode()
        return

    clean_body = {k: v for k, v in body.items() if k not in {"decompute", "yellowstone"}}
    clean_body = maybe_apply_prompt_cache(clean_body, prompt_cache_mode)
    headers = {
        "x-api-key": upstream_api_key,
        "anthropic-version": settings.anthropic_version,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if extra_headers:
        for key in ("anthropic-beta",):
            if key in extra_headers:
                headers[key] = extra_headers[key]
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", settings.anthropic_base_url.rstrip("/") + "/v1/messages", json=clean_body, headers=headers) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
