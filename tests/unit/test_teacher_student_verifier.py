"""Unit tests for the layered, escalation-biased verifier (Phase 2).

Covers: deterministic hard-fails, confidence caps by available evidence, the test-execution and
judge layers via injected fns, and the "no tests + no judge almost always escalates" property.
Uses the repo's asyncio.run convention.
"""
from __future__ import annotations

import asyncio

from decompute_gateway.teacher_student import verifier as V


GOOD_ANSWER = "Here:\n```python\ndef add(a, b):\n    return a + b\n```"
THRESHOLD = 0.82


def _verify(**kw):
    defaults = dict(task_text="implement add(a,b)", answer=GOOD_ANSWER, tests=None, threshold=THRESHOLD)
    defaults.update(kw)
    return asyncio.run(V.verify(**defaults))


# ── Deterministic hard-fails ───────────────────────────────────────────────────


def test_empty_answer_escalates():
    r = _verify(answer="")
    assert r.escalated and r.escalation_reason == "empty_answer"


def test_refusal_escalates():
    r = _verify(answer="I'm sorry, I can't help with that.")
    assert r.escalated and r.escalation_reason == "refusal"


def test_coding_task_without_code_escalates():
    r = _verify(answer="Sure, you just add the two numbers together.")
    assert r.escalated and r.escalation_reason == "missing_code"


def test_python_syntax_error_escalates():
    bad = "```python\ndef add(a, b)\n    return a + b\n```"  # missing colon
    r = _verify(answer=bad)
    assert r.escalated and r.escalation_reason == "py_syntax"


def test_truncated_answer_escalates():
    # Opening fence, no closing fence -> a cut-off generation, flagged as truncated (not missing_code).
    r = _verify(answer="```python\ndef add(a, b):\n    return")
    assert r.escalated and r.escalation_reason == "truncated"


# ── Confidence caps ────────────────────────────────────────────────────────────


def test_no_tests_no_judge_escalates_via_cap():
    """The signature property: a syntactically fine answer with no tests and no judge is capped at
    0.55 < 0.82, so it escalates anyway."""
    r = _verify()
    assert r.escalated
    assert r.escalation_reason == "low_confidence_no_evidence"
    assert r.confidence <= V.CAP_NO_EVIDENCE


def test_tests_provided_but_no_sandbox_caps_to_060():
    r = _verify(tests="assert add(2,3)==5")  # no run_tests_fn injected
    assert r.escalated
    assert r.confidence <= V.CAP_SANDBOX_UNAVAILABLE


def test_tests_pass_in_sandbox_accepts():
    async def runner(*, code, tests):
        return V.TestRunResult(ran=True, passed=True, backend="container")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests="assert add(2,3)==5", threshold=THRESHOLD, run_tests_fn=runner))
    assert not r.escalated
    assert r.decision == "accept_student"
    assert r.tests_ran and r.tests_passed
    assert r.confidence >= THRESHOLD


def test_tests_fail_in_sandbox_escalates():
    async def runner(*, code, tests):
        return V.TestRunResult(ran=True, passed=False, backend="container", detail="AssertionError")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests="assert add(2,3)==6", threshold=THRESHOLD, run_tests_fn=runner))
    assert r.escalated and r.escalation_reason == "tests_failed"


def test_sandbox_cannot_run_does_not_pretend_pass():
    async def runner(*, code, tests):
        return V.TestRunResult(ran=False, passed=False, backend="disabled", detail="no sandbox")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests="assert add(2,3)==5", threshold=THRESHOLD, run_tests_fn=runner))
    assert r.escalated
    assert r.confidence <= V.CAP_SANDBOX_UNAVAILABLE


# ── Judge layer ─────────────────────────────────────────────────────────────


def test_judge_pass_accepts_at_default_threshold_no_tests():
    """The serve-on-proxy enabler: a skeptical judge passing (no user tests) lifts the cap to
    CAP_JUDGE_PASSED (0.85), which clears the 0.82 default threshold -> accept_student. This is what
    lets the /v1/messages serve path work, since real proxy requests carry no tests."""
    async def judge(*, task_text, answer, tests):
        return V.JudgeResult(passed=True, model="claude-judge")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests=None, threshold=THRESHOLD, judge_fn=judge))
    assert not r.escalated
    assert r.decision == "accept_student"
    assert r.confidence >= THRESHOLD
    assert r.judge_model == "claude-judge"
    # A judge opinion must still rank strictly below executed-and-passing tests.
    assert V.CAP_JUDGE_PASSED < V.CAP_TESTS_PASSED


def test_judge_pass_accepts_when_threshold_lowered():
    async def judge(*, task_text, answer, tests):
        return V.JudgeResult(passed=True, model="claude-judge")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests=None, threshold=0.75, judge_fn=judge))
    assert not r.escalated
    assert r.judge_model == "claude-judge"


def test_judge_flag_escalates():
    async def judge(*, task_text, answer, tests):
        return V.JudgeResult(passed=False, model="claude-judge", severity=3, detail="off-by-one")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests=None, threshold=0.5, judge_fn=judge))
    assert r.escalated and r.escalation_reason == "judge_flagged"


def test_tests_pass_skips_judge_call():
    """When tests already give a 1.0 cap, the (costly) judge is not consulted."""
    judged = {"called": False}

    async def runner(*, code, tests):
        return V.TestRunResult(ran=True, passed=True, backend="container")

    async def judge(*, task_text, answer, tests):
        judged["called"] = True
        return V.JudgeResult(passed=True, model="claude-judge")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests="assert True", threshold=THRESHOLD, run_tests_fn=runner, judge_fn=judge))
    assert not r.escalated
    assert judged["called"] is False


# ── H4: layer exceptions escalate, never crash ──────────────────────────────────


def test_test_runner_exception_escalates_not_crashes():
    async def boom(*, code, tests):
        raise RuntimeError("sandbox blew up")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests="assert add(2,3)==5", threshold=THRESHOLD, run_tests_fn=boom))
    assert r.escalated  # did not raise; capped + escalated


def test_judge_exception_escalates_not_crashes():
    async def boom(*, task_text, answer, tests):
        raise RuntimeError("judge unreachable")

    r = asyncio.run(V.verify(task_text="add", answer=GOOD_ANSWER, tests=None, threshold=0.5, judge_fn=boom))
    assert r.escalated and r.escalation_reason == "judge_unavailable"


# ── Multi-block + polyglot (Orig-2 / Orig-3) ────────────────────────────────────


def test_runnable_python_code_concatenates_all_blocks():
    ans = "```python\ndef a(): return 1\n```\nand\n```python\ndef b(): return 2\n```"
    code = V.runnable_python_code(ans)
    assert "def a()" in code and "def b()" in code


def test_runnable_python_code_none_for_non_python_block():
    ans = "```python\ndef a(): pass\n```\n```javascript\nfunction b(){}\n```"
    assert V.runnable_python_code(ans) is None  # a JS block present -> not safely runnable here
