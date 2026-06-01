# V0 Iteration Plan — Metrics + Compression Roadmap

Derived from the 2026-05-30 review with Hina (HD). Captures her two decisions
(dashboard metrics, compression roadmap) as a tracked checklist with status.

## Status legend
- ✅ done & verified
- 🟡 in this iteration (Phase 1)
- 🔭 roadmap (next iterations)

---

## Already shipped (prior iterations)

- ✅ Production auth + tenant isolation — tested end-to-end on Postgres (19 pytest + 15 live HTTP acceptance checks)
- ✅ SQLite → SQLAlchemy + Alembic + Postgres — migrated, running, verified (FK + multi-worker race bugs found & fixed)
- ✅ Hard guard against **negative compression** — compressor never inflates; passes original through when it can't shrink (surfaced because PyTorch/CCR off)
- ✅ Root-caused the earlier inflated numbers: the boss's optimizer patch had pointed compression at a non-existent `/v1/compress` endpoint → silently fell to the aggressive **fallback** compressor. Re-wired to the **rust core** (`from headroom import compress`). The "improvements" seen earlier were the fallback over-compressing (including content it shouldn't touch).

---

## Decision 1 — Dashboard metrics (Hina)

> "Never use a single cumulative average as the headline quality metric."

| # | Task | Status |
|---|---|---|
| 1 | **Latest request savings** shown first (headline) | 🟡 Phase 1A |
| 2 | **Rolling median** savings shown second | 🟡 Phase 1A |
| 3 | **Lifetime savings** as cost/value proof (tokens + $) | 🟡 Phase 1A |
| 4 | **Benchmark-by-workload** view for claims | 🟡 Phase 1A (needs 1C) |
| 5 | Remove cumulative average as the headline | 🟡 Phase 1A |

**Why:** the earlier 65% headline was a cumulative average mixing a redundant-JSON
prompt (~90%) with a real-use-case prompt (~54%). A single average misleads. Median
+ latest + by-workload tells the honest story.

## Decision 2 — Compression roadmap (Hina)

| # | Task | Status |
|---|---|---|
| 6 | **Capability detection** for CCR / AST / PyTorch | 🟡 Phase 1B |
| 7 | **Workload-class metrics** (track savings per content class) | 🟡 Phase 1C |
| 8 | **Configure iterative CCR** (salted encryption over user logs) | 🔭 Phase 2 |
| 9 | **AST-aware compressor sidecar** for source-code-heavy contexts | 🔭 Phase 2 |
| 10 | **Keep source code protected** unless AST route can safely summarize structure | 🔭 Phase 2 |
| 11 | **Re-run benchmark corpus by workload** (not global average) | 🔭 Phase 2 (after 8–9) |

**Why the gap to Headroom's README numbers:**
- Their headline (92% code search) uses **iterative CCR** reference-replacement — ours is off → we get structural compression only.
- **PyTorch missing** → Kompress-base ML text compression degraded.
- **AST/code-aware support missing** → source-code contexts can't route to the AST compressor → 40% where Headroom gets 73%.
- **Codebase exploration already matches** (42% vs 47%) because that workload is structurally compressible without CCR/AST — the tell that confirms the above.

---

## Phase 1 scope (this iteration)

### 1A. Dashboard metrics overhaul
- Add per-request savings to the response + store it (already have `tokens_saved`, `compression_ratio` per row).
- New `usage_summary` fields: `latest_savings_percent`, `median_savings_percent`, plus existing lifetime totals.
- Dashboard layout: **Latest** (headline) → **Rolling median** → **Lifetime $ saved** → **By-workload table** → capabilities.
- Drop the cumulative-average headline framing.

### 1B. Capability detection
- New `capabilities.py`: detect `rust_core`, `kompress_ml` (torch), `ast_code_aware`, `ccr_active`.
- Honest definition of `ccr_active`: enabled flag **and** encryption key set **and** wired to the active compression path. Currently **false** (rust path ignores the ccr flag) — dashboard must stop claiming "CCR: enabled".
- Surface on `/health`, `/readyz`, and the dashboard.

### 1C. Workload-class metrics
- New `workload.py`: `classify_workload(messages) -> str` → one of
  `structured_json | logs | source_code | prose | short | mixed`.
- Schema: add `workload_class` to `usage_events` via **Alembic 0002** (Postgres-correct migration; SQLite `create_all` picks it up automatically).
- Classify at request time, store per row, aggregate `by_workload` in `usage_summary`.

### Verification
- Run the real-world corpus + Headroom-replica corpus through the live Postgres gateway.
- Confirm dashboard shows latest / median / lifetime / by-workload, and capabilities report the truth.
- Adversarial review workflow on the diff (we have a track record of latent bugs SQLite hid).

---

## Phase 2 roadmap (next, ~1–2 iterations)

8. **Iterative CCR**: salted-encryption envelope over user logs/tool-output; store originals, inject reference markers, register the retrieve tool. Wire the `ccr` flag through to the rust path (or run CCR as a pre/post stage around it). Target: +5–10% on JSON/search-heavy workloads.
9. **AST sidecar**: install `headroom-ai[code]` / tree-sitter; route source-code blocks to the AST-aware compressor.
10. **Source-code protection policy**: never lossily compress source unless the AST route can produce a safe structural summary (symbols/signatures) — otherwise pass through.
11. **Benchmark by workload**: re-run the corpus, report per-workload deltas vs Headroom, never a single global average.

---

## Open decisions for Hina
- **Rolling median window**: all-time median, or last N requests (e.g. 50)? Phase 1 uses all-time per-project; trivially switchable to a window.
- **CCR encryption key management**: env-provided Fernet key for V0 (done-ready), or KMS for production? `crypto.py` already supports a single configured key with a `key_id` for future rotation.
