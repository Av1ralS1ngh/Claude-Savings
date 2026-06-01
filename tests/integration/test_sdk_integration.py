"""
§13 — SDK integration via an injected mock transport.

Drives the real `decompute_ai.Decompute` client (real resources, real request +
error-handling code) against a deterministic in-process mock of the gateway's HTTP
surface — no network, no flaky server. Exercises resource methods, typed-error
mapping, transient-failure retry, and date-windowed usage export.

The real-gateway path is covered separately by scripts/real_test.py and
scripts/load_test.py against a live server.
"""
from __future__ import annotations

import json

import httpx
import pytest

from decompute_ai import APIError, AuthError, Decompute, RateLimitError


def _make_client(handler) -> Decompute:
    return Decompute(api_key="dc_dev_local", base_url="http://gw.test", transport=httpx.MockTransport(handler))


def _gateway_handler(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    body = json.loads(request.content) if request.content else {}
    if path == "/health":
        return httpx.Response(200, json={"ok": True, "environment": "local", "mock_anthropic": True, "capabilities": {}})
    if path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5", "type": "model"}]})
    if path == "/v1/compression/preview":
        return httpx.Response(200, json={"tokens_before": 100, "tokens_after": 40, "compression_ratio": 0.4, "messages": body.get("messages", [])})
    if path == "/v1/messages":
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 10, "output_tokens": 2}})
    if path == "/v1/usage":
        return httpx.Response(200, json={"requests": 2, "raw_input_tokens": 100, "yellowstone_final_input_tokens": 40, "tokens_saved": 60, "estimated_cost_usd": 0.01, "estimated_savings_usd": 0.02})
    if path == "/v1/usage/export":
        if request.url.params.get("format") == "csv":
            return httpx.Response(200, text="id,project_id,created_at\nu1,proj_default,2026-05-30\n", headers={"content-type": "text/csv"})
        if request.url.params.get("start", "").startswith("2099"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"id": "u1", "project_id": "proj_default"}])
    if path.endswith("/limits") and method == "PUT":
        return httpx.Response(200, json={"project_id": "proj_default", "effective": body, "overrides": body})
    if path == "/v1/unauthorized":
        return httpx.Response(401, json={"error": {"message": "bad key"}})
    if path == "/v1/ratelimited":
        return httpx.Response(429, json={"error": {"message": "slow down"}})
    return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
def client() -> Decompute:
    return _make_client(_gateway_handler)


def test_resources_hit_expected_endpoints(client: Decompute):
    assert client.get("/health")["ok"] is True
    assert client.models.list()["data"][0]["id"] == "claude-sonnet-4-5"
    preview = client.compression.preview([{"role": "tool", "content": "ERROR x"}], mode="safe")
    assert preview["tokens_after"] <= preview["tokens_before"]
    resp = client.messages.create(model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}])
    assert resp["content"][0]["text"] == "OK"
    assert client.usage.summary()["requests"] == 2


def test_usage_export_csv_json_and_date_window(client: Decompute):
    csv_text = client.usage.export(format="csv")
    assert isinstance(csv_text, str) and csv_text.splitlines()[0].startswith("id,")
    rows = client.usage.export(format="json")
    assert isinstance(rows, list) and rows
    assert client.usage.export(format="json", start="2099-01-01", end="2099-12-31") == []


def test_admin_override_via_put(client: Decompute):
    res = client._request("PUT", "/v1/projects/proj_default/limits", json={"max_context_tokens": 999999})
    assert res["effective"]["max_context_tokens"] == 999999


def test_typed_error_mapping(client: Decompute):
    with pytest.raises(AuthError):
        client.get("/v1/unauthorized")
    with pytest.raises(RateLimitError):
        client.get("/v1/ratelimited")
    with pytest.raises(APIError) as exc:
        client.get("/v1/nope")
    assert exc.value.status_code == 404


def test_retry_on_transient_5xx():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"message": "warming up"}})
        return httpx.Response(200, json={"ok": True})

    client = _make_client(flaky)  # default max_retries=2 -> 3rd attempt succeeds
    assert client.get("/health")["ok"] is True
    assert calls["n"] == 3
