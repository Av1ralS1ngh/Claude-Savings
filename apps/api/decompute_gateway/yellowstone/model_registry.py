from __future__ import annotations

from .profiles import ModelProfile


MODEL_REGISTRY = {
    "claude-opus": ModelProfile(id="claude-opus", provider="anthropic", context_window_tokens=200_000, supports_prompt_cache=True, supports_tool_use=True, pricing_class="opus"),
    "claude-sonnet": ModelProfile(id="claude-sonnet", provider="anthropic", context_window_tokens=200_000, supports_prompt_cache=True, supports_tool_use=True, pricing_class="sonnet"),
    "claude-haiku": ModelProfile(id="claude-haiku", provider="anthropic", context_window_tokens=200_000, supports_prompt_cache=True, supports_tool_use=True, pricing_class="haiku"),
    "local-student-coder": ModelProfile(id="local-student-coder", provider="local", context_window_tokens=32_000, supports_prompt_cache=False, supports_tool_use=False, pricing_class="local", local_weight_gb=8.0),
}


def get_model_profile(model: str) -> ModelProfile:
    lower = (model or "").lower()
    for key, profile in MODEL_REGISTRY.items():
        if key in lower:
            return profile
    return MODEL_REGISTRY["claude-sonnet"]
