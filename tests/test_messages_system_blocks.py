"""Regression: the gateway must accept Anthropic's array-form `system` field.

Claude Code (2.1.x) sends `system` as a LIST of content blocks (text blocks, with
cache_control for prompt caching), not a plain string — the form Anthropic's Messages
API permits alongside a string. The gateway's MessagesRequest typed `system: str | None`,
so FastAPI rejected the array with HTTP 422 (string_type, loc=["body","system"]) before
the request was ever forwarded. The fix widens it to `str | list[dict] | None`, mirroring
MessageContent.content.

The model-level test is the load-bearing guard (deterministic, no app/db/env). The
integration test proves the whole route accepts it; it runs only in mock mode so it never
calls real Anthropic and never flakes on env ordering.
"""
from __future__ import annotations

import pytest

from decompute_gateway.main import MessagesRequest

# A system value shaped exactly like Claude Code 2.1.x sends it.
CLAUDE_CODE_SYSTEM = [
    {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.156;"},
    {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}},
]


def test_system_accepts_content_block_array() -> None:
    req = MessagesRequest(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=CLAUDE_CODE_SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[{"name": "x", "description": "y", "input_schema": {"type": "object"}}],
    )
    assert isinstance(req.system, list)
    assert req.system[1]["cache_control"] == {"type": "ephemeral"}


def test_system_still_accepts_string_and_none() -> None:
    assert MessagesRequest(system="You are Claude.").system == "You are Claude."
    assert MessagesRequest().system is None


def test_messages_route_accepts_claude_code_body() -> None:
    """End-to-end through the real route — proves no 422 and (in mock mode) a 200."""
    from fastapi.testclient import TestClient

    from decompute_gateway.main import app, settings

    if not settings.mock_anthropic:
        pytest.skip("integration check runs only in mock mode (no real Anthropic call)")

    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 64,
            "system": CLAUDE_CODE_SYSTEM,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "say hi"}]}],
            "tools": [{"name": "noop", "description": "n", "input_schema": {"type": "object"}}],
        },
    )
    assert resp.status_code != 422, resp.text  # the bug
    assert resp.status_code == 200, resp.text
