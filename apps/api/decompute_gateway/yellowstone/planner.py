from __future__ import annotations

import hashlib
import time
from .kv_cache import estimate_kv_cache_mb
from .profiles import InferencePlan, ModelProfile, RuntimeProfile, TrafficProfile, DeviceProfile


MODEL_HINTS = {
    "claude-opus": {"class": "highest_reasoning", "default_route": "anthropic_claude"},
    "claude-sonnet": {"class": "balanced", "default_route": "anthropic_claude"},
    "claude-haiku": {"class": "fast", "default_route": "anthropic_claude"},
}


def classify_model(model: str) -> str:
    lower = model.lower()
    for key, val in MODEL_HINTS.items():
        if key in lower:
            return val["class"]
    return "balanced"


class YellowstonePlanner:
    """Inference/cost router for Claude optimization.

    This first iteration does not host Claude weights. It plans a compression + routing
    path around Claude and optional local pre/post-processing runtimes.
    """

    def __init__(self, devices: list[DeviceProfile]):
        self.devices = devices

    def plan(
        self,
        model: str,
        traffic: TrafficProfile,
        compression_mode: str = "safe",
        prompt_cache_mode: str = "auto",
        force_route: str | None = None,
    ) -> InferencePlan:
        ratio = traffic.compressed_input_tokens / max(traffic.raw_input_tokens, 1)
        tokens_saved = max(0, traffic.raw_input_tokens - traffic.compressed_input_tokens)
        warnings: list[str] = []

        route = force_route or "anthropic_claude"
        if traffic.raw_input_tokens < 1500:
            compression_mode = "off"
            warnings.append("Compression was not necessary for this short request.")
        elif ratio > 0.85 and compression_mode != "off":
            warnings.append("Context appears weakly compressible; use prompt caching and retrieval trimming.")

        remote = next((d for d in self.devices if "anthropic_proxy" in d.runtimes), None)
        if remote is None:
            remote = DeviceProfile(
                id="dev_anthropic_remote",
                name="Anthropic Claude API",
                kind="remote_provider",
                memory_gb=10_000,
                compute_score=100,
                runtimes=["anthropic_proxy"],
            )

        runtime = RuntimeProfile(
            name="anthropic_proxy",
            kind="remote_claude",
            device_id=remote.id,
            max_context_tokens=200_000,
            supports_streaming=True,
            supports_tools=True,
            supports_prompt_cache=True,
        )

        kv_mb = estimate_kv_cache_mb(seq_len=traffic.compressed_input_tokens)
        if traffic.compressed_input_tokens > 180_000:
            warnings.append("Compressed context is near Claude's large-context range; consider retrieval-first compression.")
        if traffic.compressed_input_tokens > traffic.raw_input_tokens:
            warnings.append("Compressed context is larger than raw context; bypass compression for this request.")

        plan_id = "yp_" + hashlib.sha256(f"{model}:{time.time()}:{traffic.raw_input_tokens}".encode()).hexdigest()[:16]
        return InferencePlan(
            id=plan_id,
            model=model,
            route=route,
            selected_runtime=runtime,
            compression_mode=compression_mode,
            prompt_cache_mode=prompt_cache_mode,
            raw_input_tokens=traffic.raw_input_tokens,
            compressed_input_tokens=traffic.compressed_input_tokens,
            tokens_saved=tokens_saved,
            compression_ratio=round(ratio, 4),
            estimated_kv_cache_mb=kv_mb,
            warnings=warnings,
        )
