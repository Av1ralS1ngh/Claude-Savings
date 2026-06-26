"""Unit tests for the local coding-student provider (Phase 1).

Covers the plan's Phase 1 acceptance criteria:
  * health works with httpx.MockTransport
  * an unavailable local server raises a typed exception (never a mock answer)
  * tools are rejected
  * context overflow is rejected
  * no mock fallback outside the explicit, flagged test-only mock

Follows the repo's `asyncio.run(...)` convention (no pytest-asyncio plugin) and the MockTransport
idiom used in tests/integration/test_sdk_integration.py.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from decompute_gateway import local_provider as lp
from decompute_gateway.config import Settings


# ── Helpers ────────────────────────────────────────────────────────────────────


def _provider(handler, **overrides) -> lp.LocalStudentProvider:
    """A provider whose HTTP goes through an httpx.MockTransport driven by `handler`."""
    kwargs = dict(
        base_url="http://127.0.0.1:18080/v1",
        model="local-student-coder",
        context_window=32_000,
        max_output_tokens=256,
        max_concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    kwargs.update(overrides)
    return lp.LocalStudentProvider(**kwargs)


def _completion(text: str, *, prompt_tokens=11, completion_tokens=7, model="local-student-coder") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
        headers={"x-request-id": "req_local_1"},
    )


# ── Translation ────────────────────────────────────────────────────────────────


def test_translation_system_and_text_blocks():
    msgs = lp.to_openai_messages(
        messages=[
            {"role": "user", "content": "implement add"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ],
        system=[{"type": "text", "text": "You are a coder."}],
    )
    assert msgs[0] == {"role": "system", "content": "You are a coder."}
    assert msgs[1] == {"role": "user", "content": "implement add"}
    assert msgs[2] == {"role": "assistant", "content": "ok"}


def test_translation_rejects_tool_result_block():
    with pytest.raises(lp.LocalProviderUnsupported) as ei:
        lp.to_openai_messages(messages=[{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}])
    assert ei.value.reason == "tools_present"


def test_translation_rejects_tool_use_block():
    with pytest.raises(lp.LocalProviderUnsupported) as ei:
        lp.to_openai_messages(messages=[{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}])
    assert ei.value.reason == "tools_present"


def test_translation_rejects_non_text_block():
    with pytest.raises(lp.LocalProviderUnsupported) as ei:
        lp.to_openai_messages(messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}])
    assert ei.value.reason == "unsupported_content"


def test_reject_tools_top_level():
    with pytest.raises(lp.LocalProviderUnsupported) as ei:
        lp.reject_tools({"tools": [{"name": "search"}]})
    assert ei.value.reason == "tools_present"
    # A body with no tools passes silently.
    lp.reject_tools({"messages": []})


def test_request_has_tools_detects_top_level_and_blocks():
    assert lp.request_has_tools(body={"tools": [{"name": "x"}]}) is True
    assert lp.request_has_tools(body={"tool_choice": "auto", "messages": []}) is True
    assert lp.request_has_tools(messages=[{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]) is True
    assert lp.request_has_tools(messages=[{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}]) is True
    # Plain text request -> no tools.
    assert lp.request_has_tools(messages=[{"role": "user", "content": "hi"}]) is False
    assert lp.request_has_tools(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]) is False


# ── Health ─────────────────────────────────────────────────────────────────────


def test_health_ok_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "local-student-coder"}, {"id": "other"}]})

    prov = _provider(handler)
    out = asyncio.run(prov.health())
    assert out["ok"] is True
    assert out["models_loaded"] == ["local-student-coder", "other"]
    assert out["base_url"] == "http://127.0.0.1:18080/v1"
    assert out["context_window"] == 32_000


def test_health_unreachable_returns_not_ok_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    prov = _provider(handler)
    out = asyncio.run(prov.health())
    assert out["ok"] is False
    assert out["error"] == "unreachable"


def test_health_is_cached_within_ttl():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": []})

    prov = _provider(handler, health_ttl_s=999)

    async def run():
        a = await prov.health()
        b = await prov.health()  # served from cache
        c = await prov.health(force=True)  # bypasses cache
        return a, b, c

    asyncio.run(run())
    assert calls["n"] == 2  # first probe + the forced one; the middle call hit the cache


def test_health_never_leaks_secrets_or_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    prov = _provider(handler, api_key="super-secret-key")
    out = asyncio.run(prov.health())
    blob = json.dumps(out)
    assert "super-secret-key" not in blob
    assert "authorization" not in blob.lower()


# ── Generation success ───────────────────────────────────────────────────────


def test_chat_success_uses_provider_usage():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _completion("def add(a, b):\n    return a + b", prompt_tokens=42, completion_tokens=9)

    prov = _provider(handler)
    res = asyncio.run(prov.chat(messages=[{"role": "user", "content": "write add(a,b)"}], system="be terse"))
    assert isinstance(res, lp.LocalProviderResult)
    assert res.ok is True and res.mock is False
    assert "def add" in res.text
    assert res.input_tokens == 42 and res.output_tokens == 9  # provider usage preferred
    assert res.request_id == "req_local_1"
    # The request was a non-streaming chat-completions call with the system message first.
    assert captured["body"]["stream"] is False
    assert captured["body"]["messages"][0] == {"role": "system", "content": "be terse"}


def test_chat_falls_back_to_estimate_when_usage_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "local-student-coder",
            "choices": [{"message": {"role": "assistant", "content": "hello world"}}],
            # no usage block
        })

    prov = _provider(handler)
    res = asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    assert res.input_tokens >= 1 and res.output_tokens >= 1  # estimate_tokens fallback


def test_chat_respects_max_tokens_override():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _completion("x")

    prov = _provider(handler)
    asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=99))
    assert captured["body"]["max_tokens"] == 99


# ── Generation failures: always typed, never mocked ──────────────────────────


def test_chat_http_500_raises_unavailable_not_mock():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error: prompt was '...secret...'")

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderUnavailable) as ei:
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    # The raw upstream body (which may echo the prompt) is not in the exception message.
    assert "secret" not in str(ei.value)
    assert ei.value.reason == "student_unavailable"


def test_chat_connect_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderUnavailable):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))


def test_chat_timeout_raises_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderTimeout) as ei:
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))
    assert ei.value.reason == "student_timeout"


def test_chat_empty_completion_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("   ")  # whitespace only

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderUnavailable):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))


def test_chat_unparseable_body_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderUnavailable):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}]))


def test_chat_over_context_rejected_before_any_network_call():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _completion("never reached")

    prov = _provider(handler, context_window=10)  # tiny window
    huge = "word " * 5000
    with pytest.raises(lp.LocalProviderOverContext):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": huge}]))
    assert calls["n"] == 0  # never hit the wire


def test_chat_with_tools_rejected_before_network():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _completion("never reached")

    prov = _provider(handler)
    with pytest.raises(lp.LocalProviderUnsupported):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]))
    assert calls["n"] == 0


# ── No silent mock fallback anywhere ─────────────────────────────────────────


def test_module_has_no_failure_fallback_to_mock():
    """The provider must never call mock_chat from a failure path. mock_chat is only reachable when a
    caller explicitly opts in via the flag — proven structurally so a future refactor can't sneak a
    fallback in."""
    import inspect

    src = inspect.getsource(lp.LocalStudentProvider)
    assert "mock_chat" not in src
    assert "mock_provider_response" not in src  # the reference bundle's hazard, never imported


def test_explicit_mock_is_flagged_and_validates_request():
    res = asyncio.run(lp.mock_chat(model="m", messages=[{"role": "user", "content": "hi"}]))
    assert res.mock is True
    assert "not a real model" in res.text.lower()
    # Even the mock refuses a tool turn (it runs the same translation as the real path).
    with pytest.raises(lp.LocalProviderUnsupported):
        asyncio.run(lp.mock_chat(model="m", messages=[{"role": "user", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}]))


# ── from_settings wiring ─────────────────────────────────────────────────────


def test_from_settings_maps_all_fields():
    s = Settings(
        local_student_base_url="http://127.0.0.1:9999/v1",
        local_student_model="m-coder",
        local_student_context_window=8000,
        local_student_max_concurrency=3,
    )
    prov = lp.LocalStudentProvider.from_settings(s)
    assert prov.base_url == "http://127.0.0.1:9999/v1"
    assert prov.model == "m-coder"
    assert prov.context_window == 8000
    assert prov.max_concurrency == 3


def test_result_is_frozen():
    res = lp.LocalProviderResult(ok=True, text="x", model="m", input_tokens=1, output_tokens=1, latency_ms=0, request_id=None)
    with pytest.raises(Exception):
        res.text = "mutated"  # frozen dataclass


# ── H5: output budget clamp + context-includes-output ───────────────────────────


def test_max_tokens_clamped_to_configured_ceiling():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _completion("ok")

    prov = _provider(handler, max_output_tokens=256)
    asyncio.run(prov.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=99999))
    # Caller asked for 99999 but the server ceiling (256) is enforced.
    assert captured["body"]["max_tokens"] == 256


def test_context_check_includes_output_budget():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _completion("never")

    # Prompt fits the window alone, but prompt + max_output overflows -> rejected before the wire.
    prov = _provider(handler, context_window=300, max_output_tokens=256)
    with pytest.raises(lp.LocalProviderOverContext):
        asyncio.run(prov.chat(messages=[{"role": "user", "content": "word " * 60}], max_tokens=256))
    assert calls["n"] == 0


def test_queue_timeout_fails_fast_when_busy():
    """Under a backlog (the single slot is held), a queued request must fail fast with a clear 'busy'
    timeout — so a burst escalates to Claude instead of hanging behind the queue."""
    import asyncio
    from decompute_gateway.local_provider import (
        LocalStudentProvider, LocalProviderTimeout, _semaphore_for,
    )

    async def go():
        p = LocalStudentProvider(base_url="http://127.0.0.1:18080/v1", model="m",
                                 max_concurrency=1, queue_timeout_s=0.3)
        sem = _semaphore_for(p.base_url, 1)
        await sem.acquire()  # occupy the only slot
        try:
            with pytest.raises(LocalProviderTimeout) as ei:
                await p.chat(messages=[{"role": "user", "content": "hi"}], system="",
                             max_tokens=10, temperature=0.2)
            assert "busy" in str(ei.value).lower()
        finally:
            sem.release()

    asyncio.run(go())
