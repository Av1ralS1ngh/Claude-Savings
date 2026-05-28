from __future__ import annotations


def estimate_kv_cache_mb(
    seq_len: int,
    batch_size: int = 1,
    layers: int = 32,
    num_kv_heads: int = 8,
    head_dim: int = 128,
    dtype_bytes: int = 2,
) -> float:
    """Rough KV-cache estimate for local LLM serving.

    KV cache stores K and V tensors for each layer. This is not used by Claude directly,
    but Yellowstone uses it to reason about local fallback/runtime feasibility.
    """
    bytes_total = batch_size * seq_len * layers * 2 * num_kv_heads * head_dim * dtype_bytes
    return round(bytes_total / 1024**2, 2)
