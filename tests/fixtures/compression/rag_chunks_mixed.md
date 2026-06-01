# Retrieved context (RAG) — 5 chunks, mixed relevance

## Chunk 1 — score 0.91 — source: docs/architecture.md
The Decompute Gateway requires Postgres 15 in production. SQLite is local-only and
startup validation rejects it when DECOMPUTE_ENV=production. The gateway never trains
models; Claude is the reviewer, not the teacher.

## Chunk 2 — score 0.88 — source: docs/production_runbook.md
Prompt caching distinguishes three token classes: uncached input, cache-creation
input, and cache-read input. Billing must store them separately. The monthly spend
cap defaults to 250 USD and returns HTTP 402 before a budget blow-up.

## Chunk 3 — score 0.42 — source: marketing/landing.md
Decompute helps teams ship faster with less context bloat. Customers love the
dashboard. (low-relevance marketing copy, safe to drop under aggressive mode.)

## Chunk 4 — score 0.39 — source: CHANGELOG.md
v0.1.0 initial release. v0.2.0 added YSCompress Rust core. Various typo fixes.
Updated README screenshots. Bumped dependencies. (low-relevance changelog noise.)

## Chunk 5 — score 0.86 — source: docs/claude_code_setup.md
Point Claude Code at the gateway with ANTHROPIC_BASE_URL=http://127.0.0.1:8080.
Gateway model discovery uses GET /v1/models. The x-headroom-bypass header disables
compression for a single request when you need the raw context to reach Claude.
