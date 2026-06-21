from __future__ import annotations

from dataclasses import dataclass

from .profiles import TrafficProfile


@dataclass(frozen=True)
class RouteDecision:
    route: str
    explanation: str
    warnings: list[str]
    local_student_candidate: bool = False
    local_preprocess_candidate: bool = False
    coding_intent: str | None = None


# Coding intents the student may attempt. These are emitted by teacher_student.intent — NOT by
# workload.py. The original policy gated on these names but read them from traffic.workload_class,
# which never emits them, so the gate was always false (dead route). The fix is to take the intent
# from a dedicated classifier (passed in as `coding_intent`).
_STUDENT_ELIGIBLE_INTENTS = {"simple_code_patch", "lint_fix", "docs_edit", "test_generation"}


def choose_route(
    *,
    model: str,
    traffic: TrafficProfile,
    compression_mode: str,
    prompt_cache_mode: str,
    local_preprocess_enabled: bool,
    local_student_enabled: bool,
    force_route: str | None = None,
    coding_intent: str | None = None,
    student_feasible: bool = False,
    has_tools: bool = False,
) -> RouteDecision:
    """Choose the V0 route.

    Teacher-student note: on the `/v1/messages` analytics path this can only ever return the
    OBSERVE route (`local_student_observe`) — it records "would have tried the student" and NEVER
    serves the student. Actually serving the student happens only through the command-gated
    /v1/teacher_student/* path (the TeacherStudentService), not here.
    """
    warnings: list[str] = []
    if force_route:
        return RouteDecision(route=force_route, explanation=f"Route forced by request/config: {force_route}", warnings=warnings)
    raw = traffic.raw_input_tokens
    final = traffic.final_input_tokens
    ratio = final / max(raw, 1)
    # Eligibility uses the DEDICATED coding-intent classifier, device feasibility, no tools, and a
    # small context — fixing the dead gate that read intents off workload_class.
    local_student_candidate = (
        local_student_enabled
        and not has_tools
        and (coding_intent in _STUDENT_ELIGIBLE_INTENTS)
        and student_feasible
        and final < 16_000
    )
    if local_student_candidate:
        return RouteDecision(
            route="local_student_observe",
            explanation=(
                f"Low-risk coding intent ({coding_intent}) on a feasible local device is a student "
                "candidate; the analytics path only OBSERVES — it never serves the student. Serving "
                "happens via the command-gated teacher-student API."
            ),
            warnings=warnings,
            local_student_candidate=True,
            coding_intent=coding_intent,
        )
    if local_preprocess_enabled and raw > 50_000 and ratio > 0.6:
        return RouteDecision(route="local_preprocess_then_claude", explanation="Large weakly-compressible context should use local preprocessing before Claude.", warnings=warnings, local_preprocess_candidate=True)
    if raw < 1500:
        warnings.append("Compression not necessary for this short request.")
        return RouteDecision(route="claude_only", explanation="Short request; send directly to Claude with minimal gateway processing.", warnings=warnings)
    if prompt_cache_mode != "off" and ratio > 0.75:
        return RouteDecision(route="yscompress_cache_first_claude", explanation="Context is only moderately compressible; use YSCompress plus prompt-cache planning.", warnings=warnings)
    return RouteDecision(route="yscompress_then_claude", explanation="Default V0 route: YSCompress context then forward to Claude provider.", warnings=warnings)
