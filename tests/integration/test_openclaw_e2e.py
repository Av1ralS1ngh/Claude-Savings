"""End-to-end OpenClaw integration through the gateway.

OpenClaw is an open-source agent harness that speaks the Anthropic Messages protocol. docs/openclaw.md
claims the gateway works with "real OpenClaw-shaped requests" — system prompt + tools, streaming and
non-streaming, with correct client tagging. These tests PROVE that claim end-to-end (previously only
the User-Agent bucketing was unit-tested).

Runs in mock-Anthropic mode (no real upstream call), like the other route integration tests.
"""
from __future__ import annotations

import importlib
import json

import pytest


# A realistic OpenClaw User-Agent (the harness sets one; the gateway buckets it to "openclaw").
OPENCLAW_UA = "OpenClaw/0.5 (acpx; +https://docs.openclaw.ai)"


def _app(monkeypatch):
    monkeypatch.setenv("DECOMPUTE_MOCK_ANTHROPIC", "true")
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)
    return main


@pytest.fixture(autouse=True)
def _restore():
    yield
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)


def _headers():
    return {"x-decompute-key": "dc_dev_local", "user-agent": OPENCLAW_UA, "content-type": "application/json"}


# An OpenClaw-shaped agentic request: system prompt + tool definitions + a tool-bearing conversation.
def _openclaw_body(stream: bool = False):
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "stream": stream,
        "system": "You are a coding agent. Use the provided tools to edit files.",
        "tools": [
            {
                "name": "edit_file",
                "description": "Apply an edit to a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
            {
                "name": "run_bash",
                "description": "Run a shell command",
                "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            },
        ],
        "messages": [
            {"role": "user", "content": "Add a docstring to utils.py"},
            {"role": "assistant", "content": [{"type": "text", "text": "I'll read the file first."}]},
            {"role": "user", "content": [{"type": "text", "text": "Go ahead."}]},
        ],
    }


def test_openclaw_non_streaming_request_proxies_and_tags(monkeypatch):
    """A full OpenClaw agentic request (system + tools) gets a well-formed Anthropic response and is
    tagged as the openclaw client."""
    from fastapi.testclient import TestClient

    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json=_openclaw_body(stream=False), headers=_headers())
    assert r.status_code == 200
    body = r.json()
    # The Anthropic message contract OpenClaw expects.
    assert body["type"] == "message" and body["role"] == "assistant"
    assert isinstance(body.get("content"), list)
    assert "usage" in body
    # Tagged as openclaw (the dashboard splits savings by harness).
    assert r.headers.get("x-decompute-request-id")


def test_openclaw_streaming_request_emits_canonical_sse(monkeypatch):
    """OpenClaw streams (stream:true). The gateway must emit the canonical Anthropic SSE event sequence
    so the harness's parser doesn't break."""
    from fastapi.testclient import TestClient

    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json=_openclaw_body(stream=True), headers=_headers())
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/event-stream")
    raw = r.text
    # Canonical Anthropic streaming sequence the harness depends on.
    for ev in ("message_start", "content_block_start", "content_block_delta", "content_block_stop",
               "message_delta", "message_stop"):
        assert f"event: {ev}" in raw, f"missing SSE event {ev}"
    # message_start must wrap the message object (OpenClaw, like Claude Code, fails without it).
    start_line = next(l for l in raw.splitlines() if l.startswith("data:") and "message_start" in l)
    payload = json.loads(start_line[len("data:"):].strip())
    assert payload["type"] == "message_start" and "message" in payload


def test_openclaw_count_tokens(monkeypatch):
    """Token budgeting: OpenClaw calls count_tokens before sending. The endpoint must answer."""
    from fastapi.testclient import TestClient

    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        r = c.post("/v1/messages/count_tokens", json={
            "model": "claude-sonnet-4-5",
            "system": "You are a coding agent.",
            "messages": [{"role": "user", "content": "hello"}],
        }, headers=_headers())
    assert r.status_code == 200
    assert "input_tokens" in r.json()


def test_openclaw_model_discovery(monkeypatch):
    """Model discovery: OpenClaw lists models via GET /v1/models."""
    from fastapi.testclient import TestClient

    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        r = c.get("/v1/models", headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert "data" in body and isinstance(body["data"], list)


def test_openclaw_usage_is_tagged_openclaw(monkeypatch):
    """After an OpenClaw request, the usage ledger / summary attributes it to the openclaw client —
    so the dashboard can split savings by harness."""
    from fastapi.testclient import TestClient

    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        c.post("/v1/messages", json=_openclaw_body(stream=False), headers=_headers())
        # Read the usage summary back and confirm an openclaw-tagged row exists.
        summ = c.get("/v1/usage", headers={"x-decompute-key": "dc_dev_local"})
    assert summ.status_code == 200
    # by_client is a list of {client, requests, ...}; OpenClaw traffic must be attributed to it.
    by_client = (summ.json() or {}).get("by_client") or []
    clients = {row.get("client") for row in by_client}
    assert "openclaw" in clients, f"OpenClaw traffic not tagged; saw clients: {clients}"


def test_openclaw_hosted_auth_via_x_api_key(monkeypatch):
    """The documented HOSTED path: OpenClaw (api: anthropic-messages) sends its project key on x-api-key.
    Under require_auth=true the gateway must accept a dc_ key there (it can't send x-decompute-key), and
    a non-dc key on x-api-key must NOT authenticate as a platform key. Closes the docs/code mismatch."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DECOMPUTE_MOCK_ANTHROPIC", "true")
    monkeypatch.setenv("DECOMPUTE_REQUIRE_AUTH", "true")
    main = _app(monkeypatch)
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    # Issue a real project key (what a hosted/team user would set as OpenClaw's apiKey).
    issued = main.store.create_api_key(project_id="proj_default", name="openclaw", scopes=["messages:write"])
    dc_key = issued["api_key"]
    assert dc_key.startswith("dc_")

    with TestClient(main.app) as c:
        # dc_ key on x-api-key (exactly how OpenClaw sends it) → authenticated.
        ok = c.post("/v1/messages", json=_openclaw_body(stream=False),
                    headers={"x-api-key": dc_key, "user-agent": OPENCLAW_UA, "content-type": "application/json"})
        assert ok.status_code == 200, ok.text
        # No key at all → 401 (auth is genuinely required, not silently bypassed).
        no_key = c.post("/v1/messages", json=_openclaw_body(stream=False),
                        headers={"user-agent": OPENCLAW_UA, "content-type": "application/json"})
        assert no_key.status_code == 401
