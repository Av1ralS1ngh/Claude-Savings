"""Unit tests for TeacherStudentService orchestration (Phase 2).

Drives the service with stubbed student providers and teacher callers (no network) to assert every
route: student_only, teacher_corrected, teacher_only, student_unavailable, disabled.
"""
from __future__ import annotations

import asyncio

import pytest

from decompute_gateway.config import Settings
from decompute_gateway.local_provider import (
    LocalProviderResult,
    LocalProviderTimeout,
    LocalProviderUnavailable,
)
from decompute_gateway.teacher_student import service as S
from decompute_gateway.teacher_student.schemas import TeacherStudentRunRequest


def _result(text: str) -> LocalProviderResult:
    return LocalProviderResult(
        ok=True, text=text, model="local-student-coder",
        input_tokens=20, output_tokens=10, latency_ms=100, request_id="req_x",
    )


class _StubProvider:
    """Stands in for LocalStudentProvider; returns a scripted answer or raises a scripted error."""

    def __init__(self, *, answer: str | None = None, error: Exception | None = None):
        self._answer = answer
        self._error = error
        self.calls = 0

    async def chat(self, *, messages, system, max_tokens, temperature):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _result(self._answer)

    async def health(self):
        return {"ok": True}


async def _teacher(*, messages, system):
    return S.TeacherResult(text="```python\ndef add(a,b):\n    return a+b\n```  (teacher)", input_tokens=37, output_tokens=12, model="claude-sonnet", upstream_request_id="req_teacher", latency_ms=200)


def _settings(**kw) -> Settings:
    base = dict(local_student_enabled=True, local_student_mock=False, teacher_student_confidence_threshold=0.82)
    base.update(kw)
    return Settings(**base)


def _run(service: S.TeacherStudentService, **req_kw):
    req = TeacherStudentRunRequest(task=req_kw.pop("task", "Write a Python add(a, b) function."), **req_kw)
    return asyncio.run(service.run(req))


# ── Routes ─────────────────────────────────────────────────────────────────────


def test_disabled_route_when_feature_off():
    svc = S.TeacherStudentService(settings=_settings(local_student_enabled=False), provider=_StubProvider(answer="x"), teacher_caller=_teacher)
    resp = _run(svc)
    assert resp.route == "disabled" and resp.final_answer is None


def test_student_only_when_tests_pass_in_sandbox():
    # Inject a passing test runner + enable execution so the verifier reaches cap 1.0 and accepts.
    async def runner(*, code, tests):
        from decompute_gateway.teacher_student.verifier import TestRunResult
        return TestRunResult(ran=True, passed=True, backend="container")

    svc = S.TeacherStudentService(
        settings=_settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="container"),
        provider=_StubProvider(answer="```python\ndef add(a,b):\n    return a+b\n```"),
        teacher_caller=_teacher,
        run_tests_fn=runner,
    )
    resp = _run(svc, tests="assert add(2,3)==5", execute_tests=True)
    assert resp.route == "student_only"
    assert resp.usage.teacher_called is False
    assert resp.usage.estimated_teacher_cost_avoided_usd > 0
    # The evidence tier must survive onto the response verdict (so it persists + the summary can
    # classify a tests-backed accept vs a judge-only one). Regression: _to_verdict used to drop these.
    assert resp.verdict.tests_ran is True
    assert resp.verdict.tests_passed is True
    assert resp.verdict.sandbox_backend == "container"
    assert resp.verdict.judge_model is None  # tests, not judge


def test_teacher_corrected_when_verifier_escalates():
    # Good answer but no tests/judge -> capped -> escalate -> teacher corrects.
    svc = S.TeacherStudentService(
        settings=_settings(),
        provider=_StubProvider(answer="```python\ndef add(a,b):\n    return a+b\n```"),
        teacher_caller=_teacher,
    )
    resp = _run(svc)
    assert resp.route == "teacher_corrected"
    assert resp.usage.teacher_called is True
    assert "teacher" in resp.final_answer
    assert resp.student_answer is not None  # the student's attempt is preserved


def test_teacher_routes_report_nonzero_input_tokens():
    """Regression: teacher-involved routes must record the teacher's billed input_tokens, not 0.

    Three paths: teacher_only (tools -> no student), require_teacher (no student), and
    teacher_corrected (student ran). All must surface non-zero input on the usage ledger so cost
    accounting is correct."""
    # teacher_only: a tool-bearing request -> no student -> input must come from the teacher (37).
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(answer="x"), teacher_caller=_teacher)
    r1 = asyncio.run(svc.run(TeacherStudentRunRequest(task="add", tools=[{"name": "search"}])))
    assert r1.route == "teacher_only"
    assert r1.usage.input_tokens == 37

    # require_teacher: forces the teacher with no student -> input must fall back to the teacher's.
    svc2 = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(answer="x"), teacher_caller=_teacher)
    r2 = asyncio.run(svc2.run(TeacherStudentRunRequest(task="add", require_teacher=True)))
    assert r2.route == "teacher_corrected"
    assert r2.usage.input_tokens == 37

    # teacher_corrected with a student: input from the student (20, per _StubProvider's _result).
    svc3 = S.TeacherStudentService(
        settings=_settings(),
        provider=_StubProvider(answer="```python\ndef add(a,b):\n    return a+b\n```"),
        teacher_caller=_teacher,
    )
    r3 = _run(svc3)
    assert r3.route == "teacher_corrected"
    assert r3.usage.input_tokens == 20


def test_judge_pass_serves_student_with_no_tests():
    """The PROXY serve path: no user tests, but an independent judge passes -> student_only, no teacher.

    This is the exact behavior that goes live on /v1/messages when DECOMPUTE_TS_JUDGE_ENABLED is on,
    and it was previously only covered at the verifier layer — pin it end-to-end through the service."""
    async def passing_judge(*, task_text, answer, tests):
        from decompute_gateway.teacher_student.verifier import JudgeResult
        return JudgeResult(passed=True, model="claude-judge", severity=0, detail="looks correct")

    svc = S.TeacherStudentService(
        settings=_settings(teacher_student_judge_enabled=True),
        provider=_StubProvider(answer="```python\ndef add(a,b):\n    return a+b\n```"),
        teacher_caller=_teacher,
        judge_fn=passing_judge,
    )
    resp = _run(svc)  # no tests supplied
    assert resp.route == "student_only"
    assert resp.usage.teacher_called is False
    assert resp.verdict is not None
    assert resp.verdict.decision == "accept_student"
    assert resp.verdict.confidence >= 0.82  # judge-passed cap (0.85) cleared the 0.82 threshold


def test_judge_fail_escalates_with_no_tests():
    """Counterpart: judge flags the answer -> escalate to the teacher, never serve the student."""
    async def failing_judge(*, task_text, answer, tests):
        from decompute_gateway.teacher_student.verifier import JudgeResult
        return JudgeResult(passed=False, model="claude-judge", severity=3, detail="off-by-one")

    svc = S.TeacherStudentService(
        settings=_settings(teacher_student_judge_enabled=True),
        provider=_StubProvider(answer="```python\ndef add(a,b):\n    return a-b\n```"),
        teacher_caller=_teacher,
        judge_fn=failing_judge,
    )
    resp = _run(svc)
    assert resp.route == "teacher_corrected"
    assert resp.usage.teacher_called is True


def test_teacher_only_when_tools_present():
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(answer="x"), teacher_caller=_teacher)
    req = TeacherStudentRunRequest(messages=[{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}])
    resp = asyncio.run(svc.run(req))
    assert resp.route == "teacher_only"
    assert resp.usage.teacher_called is True


def test_teacher_only_when_top_level_tools_present():
    """B1: a top-level `tools` field must route to teacher-only, not be silently dropped."""
    stub = _StubProvider(answer="x")
    svc = S.TeacherStudentService(settings=_settings(), provider=stub, teacher_caller=_teacher)
    req = TeacherStudentRunRequest(task="Write a Python add(a, b) function.", tools=[{"name": "search"}])
    resp = asyncio.run(svc.run(req))
    assert resp.route == "teacher_only"
    assert stub.calls == 0  # student never saw the tool-bearing request


def test_ineligible_intent_routes_teacher_only():
    """M2: a non-coding prompt is ineligible for the student -> teacher-only."""
    stub = _StubProvider(answer="x")
    svc = S.TeacherStudentService(settings=_settings(), provider=stub, teacher_caller=_teacher)
    resp = _run(svc, task="What is the capital of France?")
    assert resp.route == "teacher_only"
    assert stub.calls == 0


def test_require_teacher_skips_student():
    stub = _StubProvider(answer="x")
    svc = S.TeacherStudentService(settings=_settings(), provider=stub, teacher_caller=_teacher)
    resp = _run(svc, require_teacher=True)
    assert resp.route == "teacher_corrected"
    assert stub.calls == 0  # student never called


def test_student_unavailable_escalates_to_teacher():
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(error=LocalProviderUnavailable("down")), teacher_caller=_teacher)
    resp = _run(svc)
    assert resp.route == "teacher_only"
    assert resp.usage.teacher_called is True


def test_student_timeout_escalates_to_teacher():
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(error=LocalProviderTimeout("slow")), teacher_caller=_teacher)
    resp = _run(svc)
    assert resp.route == "teacher_only"


def test_student_unavailable_no_teacher_is_loud_not_mock():
    # No teacher_caller -> a down student returns student_unavailable, NEVER a fabricated answer.
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(error=LocalProviderUnavailable("down")), teacher_caller=None)
    resp = _run(svc)
    assert resp.route == "student_unavailable"
    assert resp.final_answer is None


def test_run_requires_task_or_messages():
    svc = S.TeacherStudentService(settings=_settings(), provider=_StubProvider(answer="x"), teacher_caller=_teacher)
    req = TeacherStudentRunRequest()  # neither task nor messages
    with pytest.raises(ValueError):
        asyncio.run(svc.run(req))


# ── Mock provider path (explicit flag only) ─────────────────────────────────


def test_mock_flag_uses_in_process_mock_and_escalates():
    # With the explicit mock flag, the student answers (mock), but no tests/judge -> escalate.
    svc = S.TeacherStudentService(settings=_settings(local_student_mock=True), teacher_caller=_teacher)
    resp = _run(svc)
    assert resp.route == "teacher_corrected"
    assert "not a real model" in resp.student_answer.lower()
