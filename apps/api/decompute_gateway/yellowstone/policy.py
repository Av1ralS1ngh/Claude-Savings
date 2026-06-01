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


def choose_route(*, model: str, traffic: TrafficProfile, compression_mode: str, prompt_cache_mode: str, local_preprocess_enabled: bool, local_student_enabled: bool, force_route: str | None = None) -> RouteDecision:
    warnings: list[str] = []
    if force_route:
        return RouteDecision(route=force_route, explanation=f"Route forced by request/config: {force_route}", warnings=warnings)
    raw = traffic.raw_input_tokens
    final = traffic.final_input_tokens
    ratio = final / max(raw, 1)
    local_student_candidate = local_student_enabled and traffic.workload_class in {"simple_code_patch", "lint_fix", "docs_edit", "test_generation"} and final < 16_000
    if local_student_candidate:
        return RouteDecision(route="local_student_dry_run", explanation="Low-risk coding workload is eligible for future local-student route; V0 keeps it as dry-run unless explicitly enabled.", warnings=warnings, local_student_candidate=True)
    if local_preprocess_enabled and raw > 50_000 and ratio > 0.6:
        return RouteDecision(route="local_preprocess_then_claude", explanation="Large weakly-compressible context should use local preprocessing before Claude.", warnings=warnings, local_preprocess_candidate=True)
    if raw < 1500:
        warnings.append("Compression not necessary for this short request.")
        return RouteDecision(route="claude_only", explanation="Short request; send directly to Claude with minimal gateway processing.", warnings=warnings)
    if prompt_cache_mode != "off" and ratio > 0.75:
        return RouteDecision(route="yscompress_cache_first_claude", explanation="Context is only moderately compressible; use YSCompress plus prompt-cache planning.", warnings=warnings)
    return RouteDecision(route="yscompress_then_claude", explanation="Default V0 route: YSCompress context then forward to Claude provider.", warnings=warnings)
