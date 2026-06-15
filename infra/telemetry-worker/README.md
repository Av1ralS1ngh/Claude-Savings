# Decompute telemetry Worker

Collects **anonymous, counts-only** weekly savings rollups from local gateways, so we can see total
token savings across users — **without ever receiving prompts, files, or any user content**.

## What it stores (and what it never does)

Stored, per device per ISO-week: `requests`, `tokens_saved`, `tokens_before`, `est_savings_usd`, and
coarse `os` / gateway `version`. Nothing else.

**Never** stored: prompts, messages, file contents, model inputs, the raw device id, or the client IP.
The device id is a random UUID minted on the user's machine (no hardware identifiers); the Worker keeps
only its `sha256`, used solely to de-duplicate a device's weekly row. The Worker also **rejects** any
POST that contains a content-shaped field (`prompt`, `message`, `content`, `file`, …) as defense in depth.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/savings` | A gateway's weekly rollup (counts only) → upsert into D1. |
| `GET`  | `/v1/stats` | Public aggregate totals (devices, requests, tokens saved, est. $ saved). Edge-cached 5 min. |
| `GET`  | `/` | Health. |

`POST /v1/savings` body:
```json
{ "device": "<random uuid>", "week": "2026-W24", "requests": 1234,
  "tokens_saved": 5600000, "tokens_before": 7100000, "est_savings_usd": 12.34,
  "os": "darwin", "version": "0.2.0" }
```

## Deploy

```bash
cd infra/telemetry-worker
wrangler d1 create decompute-telemetry          # paste the id into wrangler.toml database_id
wrangler d1 execute decompute-telemetry --file schema.sql --remote
wrangler deploy
```

Point the gateway at it with `DECOMPUTE_TELEMETRY_URL=https://decompute-telemetry.<account>.workers.dev`
(or bake the default into config.py). The public `/v1/stats` total can drive a live
"N tokens saved" number on the website.

## Privacy posture

Telemetry is **on by default but trivially disabled** — the gateway honors `DO_NOT_TRACK=1`,
`DECOMPUTE_TELEMETRY=off`, and `decompute telemetry off`, and shows a one-time notice explaining exactly
what is (and isn't) sent. Because the payload is counts-only and the device id is non-identifiable, this
is safe to run by default for a privacy-first product.
