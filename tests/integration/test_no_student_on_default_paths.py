"""Phase 0 regression baseline for the teacher-student work.

Invariant under protection (plan §"Non-Negotiable Invariants" #1, #2, #11):

  /v1/messages and /v1/chat/completions must behave EXACTLY as before. Normal proxy
  traffic must never invoke the local student — not even when the local-student feature
  flag is enabled. The student is reachable ONLY through the explicit command-gated
  /v1/teacher_student/* path (added in later phases).

These tests are written BEFORE the teacher-student code exists, on purpose: they pin the
current behavior so any later phase that accidentally wires the student into the default
proxy path turns red here.

Runs only in mock mode (no real Anthropic call), matching the other route integration
tests in this repo.
"""
from __future__ import annotations

import importlib

import pytest


def _reload_app_with(monkeypatch, **env):
    """Reload config + main with the given env so module-level Settings/flags pick it up.

    Settings reads env into frozen-dataclass field defaults at class-definition time, so a
    flag like local_student_enabled only changes if we reload the module under the new env.
    The autouse cleanup below restores the clean default modules afterwards."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)
    return main


@pytest.fixture(autouse=True)
def _restore_modules():
    """Reloading main under a mutated env poisons the imported module for later tests; restore it."""
    yield
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)


_CODING_PROMPT = "Write a Python function add(a, b) that returns a + b. Then add tests."


def test_messages_uses_claude_teacher_even_with_student_enabled(monkeypatch):
    """A coding prompt to /v1/messages with the student flag ON must still be answered by the
    (mock) Claude teacher — never by the local student."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("integration check runs only in mock mode (no real Anthropic call)")
    assert main.settings.local_student_enabled is True  # the flag really is on for this test

    with TestClient(main.app) as c:
        r = c.post(
            "/v1/messages",
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": _CODING_PROMPT}],
            },
        )
    assert r.status_code == 200
    body = r.json()
    # The mock CLAUDE response is the tell: it always carries this exact marker text. A local
    # student answer never would. This proves the proxy path went to the teacher, not the student.
    text = body["content"][0]["text"]
    assert "Mock Claude response from Decompute Gateway" in text
    # And the response is shaped like an Anthropic message (the proxy contract is unchanged).
    assert body["type"] == "message" and body["role"] == "assistant"


def test_chat_completions_uses_claude_teacher_even_with_student_enabled(monkeypatch):
    """Same guarantee for the OpenAI-compatible /v1/chat/completions path."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("integration check runs only in mock mode (no real Anthropic call)")

    with TestClient(main.app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": _CODING_PROMPT}],
            },
        )
    assert r.status_code == 200
    body = r.json()
    text = body["choices"][0]["message"]["content"]
    assert "Mock Claude response from Decompute Gateway" in text


def test_local_provider_call_path_is_never_hit_by_default_proxy(monkeypatch):
    """Defense in depth: if/when a `local_provider` module is added (Phase 1), the default proxy
    path must still never call into it. We assert the structural invariant today (no such call
    site exists in the proxy), and leave a spy that later phases inherit.

    Phase 0 form: there is no local_provider module yet, so the only thing to prove is that the
    proxy path produced a Claude answer (covered above) and that main.py does not import a
    student/local-provider module at module load (covered by the static guard test)."""
    # The ONLY teacher-student symbol main may expose is the sanctioned router-mount function
    # (include_teacher_student_router). The local provider must never leak into main's namespace —
    # the proxy path must not be able to reach it.
    import decompute_gateway.main as main

    assert "local_provider" not in [n.lower() for n in dir(main)]
    leaked = [n for n in dir(main) if "teacher_student" in n.lower() and n != "include_teacher_student_router"]
    assert leaked == [], f"main.py exposes unexpected teacher-student symbols: {leaked}"
