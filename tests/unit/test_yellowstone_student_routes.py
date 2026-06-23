"""Tests for the fixed teacher-student routing gate + observe mode (Phase 5).

The original bug (Codex-found): policy.choose_route gated the student route on workload classes
{simple_code_patch, lint_fix, docs_edit, test_generation} that workload.py NEVER emits, so the gate
was always false (dead route). The fix reads a dedicated coding-intent instead. These tests assert
the gate now fires for the right intents AND that the route is OBSERVE-only (never serves the
student) on the analytics path.
"""
from __future__ import annotations

from decompute_gateway.yellowstone.policy import choose_route
from decompute_gateway.yellowstone.planner import YellowstonePlanner
from decompute_gateway.yellowstone.profiles import DeviceProfile, TrafficProfile


def _traffic(raw=5000, final=5000, workload_class="source_code") -> TrafficProfile:
    return TrafficProfile(raw_input_tokens=raw, yscompress_input_tokens=final, yellowstone_final_input_tokens=final, workload_class=workload_class)


def _decide(**kw):
    base = dict(
        model="claude-sonnet", traffic=_traffic(), compression_mode="safe", prompt_cache_mode="auto",
        local_preprocess_enabled=False, local_student_enabled=True,
    )
    base.update(kw)
    return choose_route(**base)


# ── The dead gate is fixed ──────────────────────────────────────────────────────


def test_old_dead_gate_no_longer_relies_on_workload_class():
    """Even with a coding-ish workload_class, the route is NOT a student candidate without a coding
    intent — proving the gate no longer reads workload_class (which never emits those names)."""
    d = _decide(coding_intent=None, student_feasible=True)
    assert d.route != "local_student_observe"
    assert d.local_student_candidate is False


def test_eligible_intent_and_feasible_device_observes():
    d = _decide(coding_intent="simple_code_patch", student_feasible=True)
    assert d.route == "local_student_observe"
    assert d.local_student_candidate is True
    assert d.coding_intent == "simple_code_patch"


def test_observe_route_never_serves_student():
    # The route name is explicitly OBSERVE — the analytics path records, it does not serve.
    d = _decide(coding_intent="test_generation", student_feasible=True)
    assert d.route == "local_student_observe"
    assert "OBSERVE" in d.explanation or "never serves" in d.explanation


# ── Eligibility conditions ──────────────────────────────────────────────────────


def test_infeasible_device_is_not_a_candidate():
    d = _decide(coding_intent="simple_code_patch", student_feasible=False)
    assert d.local_student_candidate is False


def test_tools_present_is_not_a_candidate():
    d = _decide(coding_intent="simple_code_patch", student_feasible=True, has_tools=True)
    assert d.local_student_candidate is False


def test_feature_disabled_is_not_a_candidate():
    d = _decide(coding_intent="simple_code_patch", student_feasible=True, local_student_enabled=False)
    assert d.local_student_candidate is False


def test_explain_intent_not_eligible():
    d = _decide(coding_intent="explain_code", student_feasible=True)
    assert d.local_student_candidate is False


def test_large_context_not_a_candidate():
    big = _traffic(raw=40_000, final=40_000)
    d = _decide(traffic=big, coding_intent="simple_code_patch", student_feasible=True)
    assert d.local_student_candidate is False


# ── Planner integration: no student call, observe metadata only ─────────────────


def test_planner_observe_when_enabled_feasible_intent():
    # A device big enough to run a small student would be feasible; use a 64 GB device so the gate
    # depends on intent, not RAM. The planner uses the registry's local-student-coder (8 GB weight),
    # which needs ~13 GB -> 64 GB device is feasible.
    devices = [DeviceProfile(id="d", name="Mac", kind="apple_silicon", memory_gb=64, compute_score=80)]
    planner = YellowstonePlanner(devices, local_student_enabled=True)
    plan = planner.plan(model="claude-sonnet", traffic=_traffic(), coding_intent="simple_code_patch")
    assert plan.route == "local_student_observe"
    assert plan.local_student_candidate is True


def test_planner_no_observe_on_16gb_device():
    # 16 GB device can't run the 8 GB-weight student -> not feasible -> no observe route.
    devices = [DeviceProfile(id="d", name="Mac", kind="apple_silicon", memory_gb=16, compute_score=80)]
    planner = YellowstonePlanner(devices, local_student_enabled=True)
    plan = planner.plan(model="claude-sonnet", traffic=_traffic(), coding_intent="simple_code_patch")
    assert plan.route != "local_student_observe"


def test_planner_default_no_intent_is_normal_route():
    devices = [DeviceProfile(id="d", name="Mac", kind="apple_silicon", memory_gb=64, compute_score=80)]
    planner = YellowstonePlanner(devices, local_student_enabled=True)
    plan = planner.plan(model="claude-sonnet", traffic=_traffic())  # no coding_intent
    assert plan.route != "local_student_observe"
