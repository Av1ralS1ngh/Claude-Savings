"""Opt-in student-serve path on /v1/messages (DECOMPUTE_LOCAL_STUDENT_SERVE).

Counterpart to test_no_student_on_default_paths.py. Those tests pin that the student NEVER serves on
the proxy path by default; these pin the opt-in mechanics and its fail-open guarantees:

  * a verifier `student_only` accept is served locally as a normal Anthropic message, with NO upstream
    Claude call (the mock-Claude marker must be ABSENT);
  * anything else (escalation, tools present, student error) FALLS THROUGH to the normal Claude path —
    the student can only ever add a local fast-path, never replace or break the request.

KNOWN LIMITATION pinned by test_real_proxy_request_without_tests_escalates: a REAL /v1/messages request
carries no `tests`, so the verifier caps confidence at CAP_NO_EVIDENCE (0.55) and always escalates —
i.e. with today's verifier tuning the serve path is wired and safe but effectively never serves on the
proxy. Making it serve usefully needs a no-tests verification signal (judge cap reaching threshold, or
auto-generated sandbox tests) — a deliberate follow-up, not done here. The served-case test below
therefore INJECTS a stub service that returns student_only to exercise the serve/record/response path
itself; it does not imply real proxy traffic gets served today.

The serve path's only seam from main is build_teacher_student_service(); we monkeypatch it to return a
service wired with a stub provider + passing test runner (same technique as the unit tests), so these
run with no real model and no Docker.
"""
from __future__ import annotations

import importlib

import pytest


def _reload_app_with(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)
    # Auto-route ramp (3-D): the serve gate now also requires the project to be opted in. The serving
    # tests below want serving to fire, so enable it on the default project (percentage defaults to 100).
    try:
        main.store.set_project_serve_enabled("proj_default", True)
    except Exception:
        pass
    return main


@pytest.fixture(autouse=True)
def _restore_modules():
    yield
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)


_CODING_PROMPT = "Write a Python function add(a, b) that returns a + b."
_STUDENT_ANSWER = "```python\ndef add(a, b):\n    return a + b\n```"


def _make_service_factory(*, route, answer):
    """Return a build_teacher_student_service replacement whose service.run yields a fixed route.

    We stub `run` directly (rather than threading a real verifier) because the proxy builds the request
    WITHOUT tests, so the real verifier would always cap at 0.55 and escalate — see
    test_real_proxy_request_without_tests_escalates. This factory isolates the serve/response wiring."""
    from decompute_gateway.teacher_student import service as S
    from decompute_gateway.teacher_student.schemas import (
        RunUsage,
        TeacherStudentRunResponse,
        Verdict,
    )

    class _StubService:
        async def run(self, req):
            if route == "student_only":
                return TeacherStudentRunResponse(
                    id="tsc_stub", route="student_only", final_answer=answer,
                    student_answer=answer,
                    verdict=Verdict(decision="accept_student", confidence=0.9, threshold=0.82),
                    usage=RunUsage(student_output_tokens=10, teacher_called=False,
                                   estimated_teacher_cost_avoided_usd=0.001),
                )
            # escalation: teacher answered
            return TeacherStudentRunResponse(
                id="tsc_stub", route="teacher_corrected", final_answer="(teacher) corrected",
                usage=RunUsage(teacher_called=True),
            )

    def _factory(request, *, settings=None, teacher_call_func=None, upstream_auth_func=None, store=None, principal=None):
        return _StubService()

    return _factory


def test_student_only_is_served_on_proxy_when_serve_enabled(monkeypatch):
    """Opt-in: an accepted student answer is returned by /v1/messages with no upstream Claude call."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")
    assert main.settings.local_student_serve is True

    # Force a student_only accept by injecting a stub-backed service (no real model / Docker).
    import decompute_gateway.teacher_student.router as router_mod
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="student_only", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    body = r.json()
    text = body["content"][0]["text"]
    # Served by the student: the student's code is present and the mock-Claude marker is ABSENT.
    assert "def add(a, b)" in text
    assert "Mock Claude response from Decompute Gateway" not in text
    assert r.headers.get("x-decompute-served-by") == "local-student"
    # Still a well-formed Anthropic message (proxy contract preserved).
    assert body["type"] == "message" and body["role"] == "assistant"


def test_exo_serve_is_gated_until_eval_passes(monkeypatch):
    """V1c: exo can be measured/evaluated, but auto-serving Claude Code is closed until the §0 eval
    gate is explicitly marked passed."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
        DECOMPUTE_LOCAL_STUDENT_BACKEND="exo",
        DECOMPUTE_LOCAL_STUDENT_BASE_URL="http://127.0.0.1:52415/v1",
        DECOMPUTE_LOCAL_STUDENT_MODEL="mlx-community/qwen-coder",
        DECOMPUTE_LOCAL_STUDENT_EXO_EVAL_PASSED="false",
        DECOMPUTE_EXO_EVAL_PASSED="false",
    )
    assert main.settings.local_student_backend == "exo"
    assert main.settings.local_student_exo_eval_gate_passed is False

    import decompute_gateway.teacher_student.router as router_mod
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="student_only", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    assert r.headers.get("x-decompute-served-by") is None
    assert r.headers.get("x-decompute-student-fallback") == "exo_eval_gate_not_passed"
    assert "Mock Claude response from Decompute Gateway" in r.json()["content"][0]["text"]


def test_student_only_is_served_as_sse_for_streaming_request(monkeypatch):
    """The production case: Claude Code STREAMS. An accepted student answer on a streaming request must
    come back as Anthropic-shaped SSE (message_start ... content_block_delta ... message_stop), carrying
    the student's code, with no upstream Claude call."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    import decompute_gateway.teacher_student.router as router_mod
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="student_only", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/event-stream")
    assert r.headers.get("x-decompute-served-by") == "local-student"
    raw = r.text
    # Proper Anthropic SSE envelope the client expects.
    for ev in ("message_start", "content_block_start", "content_block_delta", "content_block_stop",
               "message_delta", "message_stop"):
        assert f"event: {ev}" in raw, f"missing SSE event {ev}"
    # The student's code rode in the content_block_delta; no upstream Claude.
    assert "def add(a, b)" in raw
    assert "Mock Claude response from Decompute Gateway" not in raw


def test_streaming_escalation_falls_through_to_claude_stream(monkeypatch):
    """A streaming request the verifier escalates must fall through to the normal Claude STREAM (not be
    served locally) — i.e. the mock-Claude stream, with no served-by header."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    import decompute_gateway.teacher_student.router as router_mod
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="teacher_corrected", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    assert r.headers.get("x-decompute-served-by") is None
    assert "Mock Claude response from Decompute Gateway" in r.text


def test_escalation_falls_through_to_claude_when_serve_enabled(monkeypatch):
    """Opt-in but the verifier escalates (tests fail) -> /v1/messages must return the Claude answer."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    import decompute_gateway.teacher_student.router as router_mod
    # route=teacher_corrected -> route != student_only -> fall through to Claude.
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="teacher_corrected", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    text = r.json()["content"][0]["text"]
    # Fell through to (mock) Claude — its marker is present, and no served-by header.
    assert "Mock Claude response from Decompute Gateway" in text
    assert r.headers.get("x-decompute-served-by") is None


def test_tools_request_never_reaches_student_even_with_serve(monkeypatch):
    """A request declaring tools is agentic — the serve guard must skip the student entirely and the
    request must go to Claude, regardless of the serve flag."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    # If the guard were wrong and called the student, this stub would serve it; assert it does NOT.
    import decompute_gateway.teacher_student.router as router_mod
    monkeypatch.setattr(router_mod, "build_teacher_student_service",
                        _make_service_factory(route="student_only", answer=_STUDENT_ANSWER))

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
            "tools": [{"name": "get_weather", "description": "x", "input_schema": {"type": "object"}}],
        })
    assert r.status_code == 200
    text = r.json()["content"][0]["text"]
    assert "Mock Claude response from Decompute Gateway" in text
    assert r.headers.get("x-decompute-served-by") is None


def test_real_proxy_request_without_tests_escalates(monkeypatch):
    """Honest limitation pin: with the REAL verifier (no stub), a normal /v1/messages request carries
    no tests, so confidence caps at CAP_NO_EVIDENCE (0.55) and the route escalates — the student never
    serves on the proxy today even with the serve flag ON. Uses the mock student so no real model runs.

    When a no-tests verification signal lands (judge cap reaching threshold, or auto-generated tests),
    THIS test should change deliberately — it is the tripwire that the serve path started serving."""
    from fastapi.testclient import TestClient

    main = _reload_app_with(
        monkeypatch,
        DECOMPUTE_MOCK_ANTHROPIC="true",
        DECOMPUTE_LOCAL_STUDENT_ENABLED="true",
        DECOMPUTE_LOCAL_STUDENT_SERVE="true",
        DECOMPUTE_LOCAL_STUDENT_MOCK="true",  # in-process mock student, no network/model
    )
    if not main.settings.mock_anthropic:
        pytest.skip("runs only in mock mode")

    with TestClient(main.app) as c:
        r = c.post("/v1/messages", json={
            "model": "claude-opus-4-8", "max_tokens": 64,
            "messages": [{"role": "user", "content": _CODING_PROMPT}],
        })
    assert r.status_code == 200
    # No tests -> verifier escalates -> falls through to (mock) Claude, NOT served by the student.
    text = r.json()["content"][0]["text"]
    assert "Mock Claude response from Decompute Gateway" in text
    assert r.headers.get("x-decompute-served-by") is None
