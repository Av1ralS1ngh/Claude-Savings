# Support Bundle Evidence

**Date:** 2026-06-02
**Git SHA:** see the commit that adds this file (`git log -1 -- docs/releases/v0.1.0-beta.1/support_bundle.md`)
**OS:** Darwin 25.5.0 (macOS, arm64)
**Release:** v0.1.0-beta.1

Closes the release-checklist item **"Support bundle excludes raw prompts/secrets."**

## Command

```bash
# CLI
decompute support bundle --canary sk-ant-FAKEKEYFORTEST --canary PLEASE_DO_NOT_INCLUDE_THIS_RAW_PROMPT

# Admin API (admin/support scope only)
curl -s -X POST http://127.0.0.1:8080/v1/support/bundle \
  -H "Content-Type: application/json" -H "x-decompute-api-key: dc_dev_local" \
  -d '{"download": false}'
```

## Result

- **Bundle path:** `~/.decompute/support/decompute-support-<UTC>.zip` (CLI) / `<data_dir>/support/...` (API)
- **Leak scan:** **PASS** for a normal bundle; **FAIL** (exit 2 / HTTP 500) when a canary string is present in collected content.
- **Tests:** **PASS** — `tests/test_support_bundle.py` (9 tests, incl. admin-only + audit), and the upstream pack's own 6 tests pass unmodified.
- **Audit:** `support_bundle.create` recorded in `audit_logs` with `metadata.leak_scan_ok` (verified live).

## Validation

| Property | Result |
|---|---|
| raw prompts included | **no** (`manifest.raw_prompts_included=false`; prompt-like fields → `[OMITTED:raw_prompt_or_payload_field]`) |
| CCR payloads included | **no** (`*ccr*payload*`, `*ccr_store*` excluded by glob; CCR lives encrypted in Postgres, never as files) |
| PII vault mappings included | **no** (`*pii_vault*`, `*pii-mapping*` excluded) |
| database dumps included | **no** (`*.db`, `*.sqlite*`, `*.parquet`, `*.duckdb` excluded) |
| `.env` included | **no** (`.env`, `.env.*` excluded; only `.env.example` is collected) |
| secrets scrubbed | **yes** (Anthropic/OpenAI/GitHub/AWS/Stripe/Slack/Google/npm/JWT/private-key/URL-creds/`Authorization`/`x-api-key`/`SECRET=` → `[REDACTED:*]`) |
| PII tokenized/redacted | **yes** (email/IP/phone/SSN/credit-card; default `tokenize` → `<email_1>` etc.) |
| leak scan | **PASS** for normal bundle; **FAIL** on canary |

## Notes / gateway-specific hardening

- The bundle's `runtime` snapshot records only the **program name**, never `sys.argv` — CLI arguments
  routinely carry secrets (`--api-key`, tokens, `--canary`), so capturing them verbatim would itself be a leak.
- The admin endpoint reuses this branch's auth: `_auth(request, "support:read")` → `AuthPrincipal`
  (`admin` scope is a wildcard); dev/local-noauth are allowed in `local`, non-admin api-keys are rejected (403).
- Store snapshot is metadata-only (`Store.support_bundle_snapshot()` returns counts + aggregate token/cost
  totals — never rows, prompts, keys, or PII).
- A leak-scan failure never returns the zip: CLI exits 2; the API returns HTTP 500 with the report, not the file.

## Post-review hardening (adversarial multi-agent review, 15 findings)

The integration was reviewed by an adversarial multi-agent pass focused on leaks. Confirmed leaks, all fixed:

- **CCR master key leak (critical):** `DECOMPUTE_CCR_ENCRYPTION_KEY` (a base64 Fernet key, opaque to value-regexes) was emitted verbatim in the env snapshot, and the leak scanner shared the same blind spot — so the bundle shipped marked "safe". Fixed: `SECRET_FIELD_RE` now matches `*_key`/`encryption_key`/`salt`/`seed`/`passphrase`; the env snapshot drops any secret-named var's value by name; and the leak scanner gained an **independent** backstop that flags a secret-named field carrying a long unredacted value (so it no longer shares the scrubber's blind spot).
- **Prompt under a benign key / in plain logs:** key-name omission alone missed prompts stored under keys like `note`/`input`/`text`. Fixed: broadened prompt key + free-text label aliases (and dropped the line-start anchor), plus a **value-length heuristic** that omits long free-text under any key (numeric metrics are unaffected).
- **Dotted credit-card / SSN:** the PII regexes only allowed space/hyphen separators. Fixed: dot separators now tokenized, with a decimal-number guard so timestamps aren't mistaken for cards.

Risks addressed: a leak-scan failure now **quarantines** the zip out of the shareable dir (not just gating the HTTP download); the API no longer accepts `pii_mode=passthrough` and treats server settings as a policy ceiling; the leak scanner's prompt-key check now mirrors the full omission set.

## Residual limitations (documented, not silently accepted)

- The leak scanner's entropy/format-agnostic detection is **targeted** (secret-named fields, known providers, canaries) — a *novel* secret format under a *benign* key with no key-name signal could still escape both scrubber and scanner. Code review + canaries remain the backstop.
- A **short** prompt (< 256 chars) stored under a truly arbitrary, non-prompt-ish key would not be omitted (long prose and prompt-ish keys are covered).
- Unstructured operator log files are scrubbed for secrets/PII but cannot be guaranteed free of arbitrary prompt *prose*; prefer structured (JSONL) logs.
