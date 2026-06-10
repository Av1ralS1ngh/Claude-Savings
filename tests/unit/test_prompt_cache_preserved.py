"""Cost guard: compression is skipped (prompt cache preserved) ONLY for very large cache-controlled
contexts, where busting the cache costs more than compression saves. Normal cache-controlled
requests (a log read, a few tool results) are still compressed — the new content compresses ~90-99%
and the cache being protected is small. Uncached requests are always compressed.

Runs only in mock mode (no real Anthropic call), matching the other route integration tests."""
import pytest


def _client():
    from fastapi.testclient import TestClient
    from decompute_gateway.main import app, settings
    if not settings.mock_anthropic:
        pytest.skip("integration check runs only in mock mode (no real Anthropic call)")
    if not settings.preserve_prompt_cache:
        pytest.skip("preserve_prompt_cache disabled")
    return TestClient(app), settings


def test_large_cache_controlled_context_is_preserved():
    client, settings = _client()
    # Exceed the preserve threshold (estimate_tokens ~= chars/4).
    big = "x" * (settings.preserve_cache_min_tokens * 4 + 100_000)
    with client as c:
        r = c.post("/v1/messages?beta=true", headers={"x-decompute-metadata": "true"}, json={
            "model": "claude-opus-4-8", "max_tokens": 16,
            "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": big}]},
                {"role": "user", "content": "summarize"},
            ],
        })
        assert r.status_code == 200
        assert r.headers.get("x-decompute-prompt-cache-preserved") == "true"
        assert r.headers.get("x-decompute-compression-ratio") == "1.0"


def test_small_cache_controlled_request_is_compressed():
    client, _ = _client()
    with client as c:
        r = c.post("/v1/messages?beta=true", headers={"x-decompute-metadata": "true"}, json={
            "model": "claude-opus-4-8", "max_tokens": 16,
            "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "ran"},
                                                  {"type": "tool_use", "id": "t", "name": "logs", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ERROR boom\n" * 800}]},
                {"role": "user", "content": "summarize"},
            ],
        })
        assert r.status_code == 200
        # Below the threshold -> compressed despite cache_control (the win is the new content).
        assert r.headers.get("x-decompute-prompt-cache-preserved") is None
        assert float(r.headers.get("x-decompute-compression-ratio", "1.0")) < 1.0


def test_uncached_request_is_compressed():
    client, _ = _client()
    with client as c:
        r = c.post("/v1/messages?beta=true", headers={"x-decompute-metadata": "true"}, json={
            "model": "claude-opus-4-8", "max_tokens": 16,
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "ran"},
                                                  {"type": "tool_use", "id": "t", "name": "logs", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ERROR boom\n" * 800}]},
                {"role": "user", "content": "summarize"},
            ],
        })
        assert r.status_code == 200
        assert r.headers.get("x-decompute-prompt-cache-preserved") is None
        assert float(r.headers.get("x-decompute-compression-ratio", "1.0")) < 1.0
