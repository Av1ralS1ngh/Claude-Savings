"""teacher_student_summary observability — the serve-canary's metrics surface.

Pins that the summary classifies accepts by VERIFICATION TIER (tests-backed cap-1.0 vs judge-only
cap-0.85) and reports the judge pass-rate. This is what an operator watches during the serve ramp,
and it depends on the verdict's tests_ran/judge_model fields actually reaching the store — a path that
previously silently dropped them (see test_student_only_when_tests_pass_in_sandbox)."""
from __future__ import annotations

from decompute_gateway.config import Settings
from decompute_gateway.db import Store


def _store(tmp_path) -> Store:
    s = Store(Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/sum.db"))
    s.create_schema_for_local_dev()
    return s


def _case(*, route, verdict):
    return dict(
        request_id="dreq",
        route=route,
        escalation_reason=verdict.get("escalation_reason"),
        prompt_sha256="a" * 64,
        verdict=verdict,
    )


def test_summary_breaks_accepts_down_by_evidence_tier(tmp_path):
    store = _store(tmp_path)
    pid = store.create_project("p")["id"]

    # A tests-backed accept (executed sandbox tests, cap 1.0).
    store.record_teacher_student_case(_case(route="student_only", verdict={
        "decision": "accept_student", "confidence": 0.85, "threshold": 0.82, "escalated": False,
        "tests_ran": True, "tests_passed": True, "sandbox_backend": "container",
    }), project_id=pid, request_capture=False)

    # A judge-only accept (no tests, judge passed, cap 0.85) — the proxy serve tier.
    store.record_teacher_student_case(_case(route="student_only", verdict={
        "decision": "accept_student", "confidence": 0.85, "threshold": 0.82, "escalated": False,
        "tests_ran": False, "judge_model": "claude-judge",
    }), project_id=pid, request_capture=False)

    # An escalation where the judge flagged it (judge consulted but failed).
    store.record_teacher_student_case(_case(route="teacher_corrected", verdict={
        "decision": "escalate", "confidence": 0.0, "threshold": 0.82, "escalated": True,
        "escalation_reason": "judge_flagged", "judge_model": "claude-judge",
    }), project_id=pid, request_capture=False)

    summ = store.teacher_student_summary(project_id=pid)

    assert summ["cases"] == 3
    assert summ["student_accepted"] == 2
    assert summ["teacher_called"] == 1
    assert summ["by_route"] == {"student_only": 2, "teacher_corrected": 1}

    # Evidence-tier split of the 2 accepts.
    ev = summ["accept_evidence"]
    assert ev["tests_backed"] == 1
    assert ev["judge_only"] == 1
    assert ev["no_evidence"] == 0  # invariant: no accept without evidence

    # Judge consulted on 2 cases (the judge-only accept + the judge-flagged escalation); passed 1.
    j = summ["judge"]
    assert j["consulted"] == 2
    assert j["passed"] == 1
    assert j["pass_rate"] == 0.5


def test_summary_empty_project_is_zeroed(tmp_path):
    store = _store(tmp_path)
    pid = store.create_project("empty")["id"]
    summ = store.teacher_student_summary(project_id=pid)
    assert summ["cases"] == 0
    assert summ["accept_evidence"]["tests_backed"] == 0
    assert summ["judge"]["pass_rate"] == 0.0
