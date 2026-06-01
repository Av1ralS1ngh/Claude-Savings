from __future__ import annotations

from dataclasses import asdict
import hashlib
import time

from .kv_cache import estimate_kv_cache_mb
from .model_registry import get_model_profile
from .policy import choose_route
from .profiles import DeviceProfile, InferencePlan, OptimizationStep, RuntimeProfile, TrafficProfile


class YellowstonePlanner:
    """V0 planner/router.

    This class is intentionally pure-Python and imports no legacy Ray/PyTorch Yellowstone runtime.
    """

    def __init__(self, devices: list[DeviceProfile], *, planner_version: str = "v0.1", policy_version: str = "v0.1", local_preprocess_enabled: bool = False, local_student_enabled: bool = False):
        self.devices = devices
        self.planner_version = planner_version
        self.policy_version = policy_version
        self.local_preprocess_enabled = local_preprocess_enabled
        self.local_student_enabled = local_student_enabled

    def plan(self, model: str, traffic: TrafficProfile, compression_mode: str = "safe", prompt_cache_mode: str = "auto", force_route: str | None = None, optimization_steps: list[OptimizationStep] | None = None) -> InferencePlan:
        final_tokens = traffic.final_input_tokens
        ys_tokens = traffic.yscompress_input_tokens
        raw_tokens = traffic.raw_input_tokens
        ys_saved = max(0, raw_tokens - ys_tokens)
        yellowstone_saved = max(0, ys_tokens - final_tokens)
        total_saved = max(0, raw_tokens - final_tokens)
        # Clamp ratio to <= 1.0. Compression never ships more than the raw
        # context (the compressor passes the original through when it cannot
        # shrink it), so reported savings must floor at 0%, never go negative.
        ratio = min(1.0, final_tokens / max(raw_tokens, 1))
        warnings: list[str] = []
        model_profile = get_model_profile(model)
        decision = choose_route(
            model=model,
            traffic=traffic,
            compression_mode=compression_mode,
            prompt_cache_mode=prompt_cache_mode,
            local_preprocess_enabled=self.local_preprocess_enabled,
            local_student_enabled=self.local_student_enabled,
            force_route=force_route,
        )
        warnings.extend(decision.warnings)
        remote = next((d for d in self.devices if "anthropic_proxy" in d.runtimes), None) or DeviceProfile(id="dev_anthropic_remote", name="Anthropic Claude API", kind="remote_provider", memory_gb=10_000, compute_score=100, runtimes=["anthropic_proxy"])
        runtime = RuntimeProfile(name="anthropic_proxy", kind="remote_claude", device_id=remote.id, max_context_tokens=model_profile.context_window_tokens, supports_streaming=True, supports_tools=model_profile.supports_tool_use, supports_prompt_cache=model_profile.supports_prompt_cache)
        if final_tokens > model_profile.context_window_tokens:
            warnings.append(f"Final context exceeds model context window: {final_tokens}>{model_profile.context_window_tokens}")
        if ys_tokens > raw_tokens:
            warnings.append("YSCompress output is larger than raw context; bypass compression for this request.")
        kv_mb = estimate_kv_cache_mb(seq_len=final_tokens)
        plan_id = "yp_" + hashlib.sha256(f"{model}:{time.time()}:{raw_tokens}:{final_tokens}".encode()).hexdigest()[:16]
        return InferencePlan(
            id=plan_id,
            model=model,
            route=decision.route,
            selected_runtime=runtime,
            compression_mode=compression_mode,
            prompt_cache_mode=prompt_cache_mode,
            raw_input_tokens=raw_tokens,
            yscompress_input_tokens=ys_tokens,
            yellowstone_final_input_tokens=final_tokens,
            tokens_saved=total_saved,
            yscompress_tokens_saved=ys_saved,
            yellowstone_tokens_saved=yellowstone_saved,
            compression_ratio=round(ratio, 4),
            final_savings_percent=round((1.0 - ratio) * 100, 2),
            estimated_kv_cache_mb=kv_mb,
            planner_version=self.planner_version,
            policy_version=self.policy_version,
            model_profile=asdict(model_profile),
            explanation=decision.explanation,
            local_student_candidate=decision.local_student_candidate,
            local_preprocess_candidate=decision.local_preprocess_candidate,
            optimization_steps=optimization_steps or [],
            warnings=warnings,
        )
