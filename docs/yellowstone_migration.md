# Yellowstone migration plan

This bundle ships a clean v0 Yellowstone planner under:

```text
apps/api/decompute_gateway/yellowstone/
```

Legacy research code is copied under:

```text
research/yellowstone_legacy/
```

## Migration order

1. `device_profiling.py` → production local/remote device scanner.
2. `model_memory_estimation.py` → model profile loader and local KV/weight fit estimator.
3. `model_distribution.py` and `balanced_clustering.py` → heterogeneous placement planner.
4. `shard_inference.py` + `worker.py` → optional local sharded inference backend.
5. `optimized_interleaved.py` → later training/local pipeline runtime, not v0 Claude gateway.

## v0 boundary

The v0 Claude gateway does not train models and does not import Ray/PyTorch runtime code. It optimizes request context before Claude and records Yellowstone routing decisions.
