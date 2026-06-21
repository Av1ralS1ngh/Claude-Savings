"""Tests for the coding-intent classifier and the device-feasibility gate (Phase 5)."""
from __future__ import annotations

from decompute_gateway.teacher_student import intent as I
from decompute_gateway.yellowstone.profiles import DeviceProfile, ModelProfile
from decompute_gateway.yellowstone.student_capability import assess_local_student, required_ram_gb


# ── Coding-intent classifier ────────────────────────────────────────────────────


def test_intent_test_generation():
    assert I.classify_coding_intent("Write unit tests for add(a, b)") == "test_generation"
    assert I.classify_coding_intent("add pytest cases asserting the output") == "test_generation"


def test_intent_lint_fix():
    assert I.classify_coding_intent("fix the ruff lint errors in this file") == "lint_fix"
    assert I.classify_coding_intent("run black to format this") == "lint_fix"


def test_intent_docs_edit():
    assert I.classify_coding_intent("add a docstring to this function") == "docs_edit"


def test_intent_simple_code_patch():
    assert I.classify_coding_intent("implement a function add(a, b) that returns a+b") == "simple_code_patch"
    assert I.classify_coding_intent("fix the bug in ```def f(): return 1```") == "simple_code_patch"


def test_intent_explain_code():
    assert I.classify_coding_intent("explain what this regex does") == "explain_code"


def test_intent_unknown_for_non_coding():
    assert I.classify_coding_intent("what's the weather in Paris?") == "unknown"
    assert I.classify_coding_intent("") == "unknown"


def test_eligible_intents():
    assert I.is_student_eligible_intent("test_generation")
    assert I.is_student_eligible_intent("simple_code_patch")
    assert not I.is_student_eligible_intent("explain_code")  # excluded by default
    assert not I.is_student_eligible_intent("unknown")


# ── Device feasibility (the 16 GB Mac case) ─────────────────────────────────────


def _local_model(weight_gb: float) -> ModelProfile:
    return ModelProfile(id="local-student-coder", provider="local", context_window_tokens=32_000, local_weight_gb=weight_gb)


def _device(mem_gb: float, kind: str = "apple_silicon") -> DeviceProfile:
    return DeviceProfile(id="dev1", name="Mac", kind=kind, memory_gb=mem_gb, compute_score=50)


def test_required_ram_includes_headroom():
    assert required_ram_gb(_local_model(7.4)) > 7.4  # headroom factor applied


def test_16gb_mac_cannot_run_12b_q4():
    # 12B Q4_K_M ~ 7.4 GB weight -> ~11.8 GB needed; 16 GB device has ~12 GB usable after OS reserve.
    verdict = assess_local_student(device=_device(16), model=_local_model(7.4))
    assert verdict.feasible is False
    assert verdict.reason == "insufficient_ram"


def test_32gb_mac_can_run_12b_q4():
    verdict = assess_local_student(device=_device(32), model=_local_model(7.4))
    assert verdict.feasible is True


def test_small_coder_fits_on_16gb():
    # A small coder (~3 GB) fits comfortably on 16 GB.
    verdict = assess_local_student(device=_device(16), model=_local_model(3.0))
    assert verdict.feasible is True


def test_no_device_is_infeasible():
    verdict = assess_local_student(device=None, model=_local_model(3.0))
    assert verdict.feasible is False and verdict.reason == "no_local_device"


def test_remote_device_is_infeasible():
    remote = DeviceProfile(id="r", name="Claude", kind="remote_provider", memory_gb=10_000, compute_score=100)
    verdict = assess_local_student(device=remote, model=_local_model(3.0))
    assert verdict.feasible is False


def test_remote_model_is_not_a_local_student():
    remote_model = ModelProfile(id="claude-sonnet", provider="anthropic")
    verdict = assess_local_student(device=_device(64), model=remote_model)
    assert verdict.feasible is False and verdict.reason == "model_not_local"
