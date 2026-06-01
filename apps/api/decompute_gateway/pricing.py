from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus": ModelPrice(input_per_mtok=15.0, output_per_mtok=75.0, cache_write_per_mtok=18.75, cache_read_per_mtok=1.5),
    "claude-sonnet": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0, cache_write_per_mtok=3.75, cache_read_per_mtok=0.3),
    "claude-haiku": ModelPrice(input_per_mtok=0.8, output_per_mtok=4.0, cache_write_per_mtok=1.0, cache_read_per_mtok=0.08),
    "default": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0, cache_write_per_mtok=3.75, cache_read_per_mtok=0.3),
}


def price_for_model(model: str | None) -> ModelPrice:
    lower = (model or "").lower()
    for key, price in DEFAULT_PRICES.items():
        if key != "default" and key in lower:
            return price
    return DEFAULT_PRICES["default"]


def cost_usd(*, model: str | None, input_tokens: int, output_tokens: int, cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0) -> float:
    p = price_for_model(model)
    uncached = max(0, input_tokens - cache_creation_input_tokens - cache_read_input_tokens)
    total = (
        uncached / 1_000_000 * p.input_per_mtok
        + output_tokens / 1_000_000 * p.output_per_mtok
        + cache_creation_input_tokens / 1_000_000 * p.cache_write_per_mtok
        + cache_read_input_tokens / 1_000_000 * p.cache_read_per_mtok
    )
    return round(total, 8)


def savings_usd(*, model: str | None, raw_input_tokens: int, final_input_tokens: int, output_tokens: int, cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0) -> float:
    raw_cost = cost_usd(model=model, input_tokens=raw_input_tokens, output_tokens=output_tokens)
    final_cost = cost_usd(
        model=model,
        input_tokens=final_input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    return round(max(0.0, raw_cost - final_cost), 8)
