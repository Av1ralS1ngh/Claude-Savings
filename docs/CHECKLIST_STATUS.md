# V0 P0/P1 Checklist — Status

Status of the delivery checklist (§4–§18) against the actual gateway code.
Legend: ✅ done & verified · 🟡 partial (gap noted) · ⛔ not started.

Verification: `pytest -q` → **104 passed**; live runs of `scripts/load_test.py`,
`scripts/claude_code_compat_suite.py`, and `decompute doctor` against a mock-mode
gateway all green.

| § | Section | Status | Evidence / remaining gap |
|---|---|---|---|
| 4 | Claude API compat hardening | ✅ | request/response/error passthrough, `/v1/models`, `count_tokens`, model allowlist, beta-header allowlist. **Added:** upstream `request-id` now captured on the streaming path (`sse.py`, `anthropic_provider.py`, `main.py`). |
| 5 | Streaming/SSE correctness | ✅ | parser, output-token accounting, tool/thinking/ping passthrough, mid-stream error, disconnect. **Added:** stream tests for long output, mid-stream error, partial buffering, mock-stream request-id (`tests/integration/test_messages_streaming.py`). |
| 6 | Rate limits / quotas / spend | ✅ | per-key RPM, tokens/min, monthly cap (402), max size/context, concurrent streams, retry-after. **Added:** admin quota-override `PUT/GET/DELETE /v1/projects/{id}/limits` (`rate_limits.py`, `main.py`, `tests/unit/test_rate_limit_override.py`). |
| 7 | Production CORS / dashboard | ✅ | env allowlist, no wildcard+creds in prod, dashboard auth, CSRF header, read-only mode, security headers. *Remaining:* a committed HTTP-origin acceptance test (behaviour verified manually). |
| 8 | Observability / tracing | 🟡 | JSON logs (request_id/project_id), `/metrics` Prometheus, usage-event detail, upstream status capture. *Remaining:* dedicated error/latency-percentile/cache-hit dashboards (raw metrics exist; `load_test.py` now reports p50/p95/p99). |
| 9 | Billing / savings | ✅ | per-model pricing incl. cache write/read, full usage-event detail, savings model, CSV/JSON export, by-model/workload rollups, cost guardrails. **Added:** `/v1/usage/export` now accepts `start`/`end` date window. |
| 10 | Compression quality gates | 🟡 | tests/policies/modes/bypass done; **9/9 golden fixtures now present** (see §17). *Remaining:* CI jobs (no CI configured) and a `debug` mode. |
| 11 | Yellowstone V0 planner | ✅ | device scanner, 5-route policy engine, plan versioning, model registry, explainability, no-legacy-import test, no training/Ray/PyTorch. **Fixed:** removed the orphaned, superseded `ys_optimizer_patch` modules + stale test that were breaking collection. |
| 12 | Deployment hardening | 🟡 | `/readyz`, `/livez`, prod config-validation (blocks mock/no-auth/wildcard/SQLite/missing-CCR-key), rollback doc. *Remaining:* Dockerfile hardening (non-root, healthcheck, multi-worker), secret-manager integration, staging config. |
| 13 | SDK/CLI polish | ✅ | **Added:** CLI `doctor`, `login`, `config-set`, `usage --from/--to/--format`; typed error mapping (`AuthError`/`RateLimitError`/…), transient-5xx retry, injectable transport, `Usage`/`Models` resources, config-file fallback; SDK integration test (`tests/integration/test_sdk_integration.py`). |
| 14 | Web dashboard V0 | 🟡 | live metrics (latest/median/lifetime $, by-workload), compression view, auth gate. *Remaining by design (Hina metrics rework):* admin UI pages for CCR / playground / route-planner / settings (all endpoints exist). |
| 15 | Claude Code compat suite | ✅ | **Added:** `scripts/claude_code_compat_suite.py` — all 10 checkpoints, **10/10 live**. |
| 16 | Error handling / retry | ✅ | 400/401-403/413/429 + retry-after + stream-error forwarding. **Added:** 500/504/529 retry-with-backoff and 401/403 body sanitization (request-id kept for tracing) — `anthropic_provider.py`, `tests/unit/test_provider_retry.py`. |
| 17 | Compression safety evaluator | ✅ | **Added:** the 7 missing fixtures (9/9 total) + `tests/unit/test_compression_evaluator.py` — per-fixture raw/compressed tokens, ratio, critical-fact preservation, latest-user verbatim, secret-redaction gate. |
| 18 | Load and soak testing | ✅ | **Added:** `scripts/load_test.py` — concurrent non-stream/stream, sustained RPM, huge-context, bypass, 429-shed, client-disconnect, with p50/p95/p99 vs V0 targets. |

## What changed this session

- **Removed dead code** (boss's planner rewrite superseded it): `yellowstone/optimizer.py`,
  `segments.py`, `conversation_memory.py`, `log_optimizer.py`, `source_optimizer.py`,
  and `tests/test_yellowstone_optimizer.py`. Set `testpaths = ["tests"]` in `pyproject.toml`
  so `scripts/*_test.py` are no longer mis-collected. Suite went from **3 collection errors → 104 passing**.
- **§17/§18/§13/§6/§15/§16/§4** built additively (new files + extensions), no rewrites of
  the evolved/redirected gateway code.

## Known remaining (not in this session's agreed scope)

§8 error/latency/cache dashboards · §10 CI jobs + `debug` mode · §12 Dockerfile hardening,
secret-manager, staging · §14 admin UI pages · §7 committed CORS acceptance test.
