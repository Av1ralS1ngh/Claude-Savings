# Architecture

```text
Claude Code / app / SDK
        ↓
Decompute Gateway
  - API key / project shell
  - Headroom context compression
  - CCR original-content store
  - prompt-cache optimizer
  - Yellowstone route planner
  - usage and savings telemetry
        ↓
Anthropic Claude API
```

## Why no Echelon in v0?

Echelon is training/adaptation infrastructure. This product iteration is inference/request optimization around Claude.

## Yellowstone role in v0

Yellowstone is the routing brain, not a model server yet.

It profiles devices, estimates compressed token footprint, estimates local KV cache implications, and records routing plans. The legacy Yellowstone docs emphasize resource-aware device/model profiling, communication cost, clustering, backup devices, heterogeneous devices, and Ray + PyTorch as a path for heterogeneous execution. In this platform iteration, we reuse the idea for Claude gateway routing and defer the heavier distributed training/inference runtime.

## Headroom role in v0

Headroom is the context compressor. It should compress logs, tool outputs, RAG chunks, JSON/API responses, and old conversation context before they hit Claude. The gateway falls back to a deterministic local compressor if Headroom is not running.

## Claude role in v0

Claude remains provider-hosted. The gateway forwards to `/v1/messages` after compression and prompt-cache adjustment.

## Future runtime modes

1. Anthropic Claude proxy.
2. Local summarizer/reranker preprocessor.
3. Local fallback generation with llama.cpp or MLX on Mac.
4. Linux GPU backend with vLLM or SGLang.
5. Yellowstone heterogeneous inference planner.
