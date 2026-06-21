"""Integration tests for the teacher-student HTTP surface (Phase 2).

Exercises the real router -> service -> verifier path via TestClient with the explicit in-process
student mock and the mock Anthropic teacher. Asserts the plan's status-code rules and that the
default proxy path is still untouched.
"""
from __future__ import annotations

import importlib

import pytest


def _reload_with(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)
    return main


@pytest.fixture(autouse=True)
def _restore_modules():
    yield
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)


def _client(monkeypatch, **env):
    from fastapi.testclient import TestClient

    env.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "true")
    main = _reload_with(monkeypatch, **env)
    if not main.settings.mock_anthropic:
        pytest.skip("integration check runs only in mock mode")
    return TestClient(main.app), main


# ── health ─────────────────────────────────────────────────────────────────────


def test_health_reports_config_without_secrets(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.get("/v1/teacher_student/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"enabled", "student", "verifier", "sandbox", "governance", "student_health"}
    assert "api_key" not in str(body).lower()


# ── run: disabled ───────────────────────────────────────────────────────────────


def test_run_disabled_returns_503(monkeypatch):
    # Feature off (no mock, not enabled) -> 503 teacher_student_disabled.
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_ENABLED="false", DECOMPUTE_LOCAL_STUDENT_MOCK="false")
    with client as c:
        r = c.post("/v1/teacher_student/run", json={"task": "Write add(a,b)", "source": "cli", "command": "ts-run"})
    assert r.status_code == 503
    assert r.json()["error"] == "teacher_student_disabled"


# ── run: escalation to teacher (mock student has no test evidence) ──────────────


def test_run_escalates_to_teacher_corrected(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.post("/v1/teacher_student/run", json={
            "task": "Write a Python add(a, b) function.",
            "tests": "assert add(2,3)==5",
            "source": "cli", "command": "ts-run",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "teacher_corrected"
    assert body["usage"]["teacher_called"] is True
    assert body["verdict"]["decision"] == "escalate"
    # The teacher's answer (mock Claude) is the final answer.
    assert "Mock Claude response from Decompute Gateway" in body["final_answer"]


def test_run_require_teacher(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.post("/v1/teacher_student/run", json={"task": "Write add(a,b)", "require_teacher": True, "source": "cli", "command": "ts-run"})
    assert r.status_code == 200
    assert r.json()["route"] == "teacher_corrected"


def test_run_tools_present_routes_teacher_only(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.post("/v1/teacher_student/run", json={
            "messages": [{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}],
            "source": "cli", "command": "ts-run",
        })
    assert r.status_code == 200
    assert r.json()["route"] == "teacher_only"


def test_run_invalid_request_400(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.post("/v1/teacher_student/run", json={"source": "cli", "command": "ts-run"})  # no task/messages
    assert r.status_code == 400


# ── local-chat (direct, unverified) ─────────────────────────────────────────────


def test_local_chat_is_labeled_unverified(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.post("/v1/teacher_student/local-chat", json={"prompt": "draft add(a,b)"})
    assert r.status_code == 200
    body = r.json()
    assert body["unverified"] is True
    assert body["answer"]


def test_local_chat_disabled_503(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_ENABLED="false", DECOMPUTE_LOCAL_STUDENT_MOCK="false")
    with client as c:
        r = c.post("/v1/teacher_student/local-chat", json={"prompt": "hi"})
    assert r.status_code == 503


# ── invariant: the default proxy path is untouched even with the feature on ─────


def test_messages_path_unchanged_with_feature_on(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true", DECOMPUTE_LOCAL_STUDENT_ENABLED="true")
    with client as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 32,
            "messages": [{"role": "user", "content": "Write add(a,b) and tests."}],
        })
    assert r.status_code == 200
    # Still the Claude teacher, not the student.
    assert "Mock Claude response from Decompute Gateway" in r.json()["content"][0]["text"]


# ── read / export surfaces ──────────────────────────────────────────────────────


def test_summary_endpoint(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.get("/v1/teacher_student/summary")
    assert r.status_code == 200
    assert set(r.json()) >= {"cases", "student_accepted", "teacher_called", "escalation_rate"}


def test_export_409_when_metadata_only(monkeypatch):
    client, _ = _client(monkeypatch, DECOMPUTE_LOCAL_STUDENT_MOCK="true")
    with client as c:
        r = c.get("/v1/teacher_student/export/sft.jsonl")
    # Default project is metadata-only -> nothing to export.
    assert r.status_code == 409
    assert r.json()["error"] == "export_forbidden"
