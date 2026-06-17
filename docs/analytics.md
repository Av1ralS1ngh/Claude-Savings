# Analytics & telemetry

Decompute collects **anonymous, counts-only** telemetry so we can see aggregate adoption and savings
across users — **without ever receiving prompts, files, model inputs, or any user content**. Everything
lands in one Cloudflare D1 database behind one Worker; reads are internal-only.

## What flows, and from where

| Signal | Source | Endpoint | Table |
|---|---|---|---|
| **Token savings** (weekly) | the gateway (`decompute_gateway/telemetry.py`) | `POST /v1/savings` | `savings` |
| **Downloads** (installs) | the curl installer (`scripts/install.sh`) | `POST /v1/event` | `downloads` |
| **Website visits** | the marketing site (`website/pro/index.html`) | `POST /v1/visit` | `visits` |
| **Internal dashboard** | us (Bearer-gated) | `GET /v1/stats` | (reads all three) |

The gateway's weekly savings sender is `telemetry.py` — it is the ONLY thing the gateway phones home,
honors `DO_NOT_TRACK` and the `decompute telemetry off` CLI toggle, and runs fire-and-forget.

## Privacy contract (sacred)

- Stored: counts, coarse `os` (`darwin`/`linux`/`win32`/`unknown`), gateway/CLI `version`, and **hashes**.
- **Never** stored: prompts, messages, files, model inputs, raw device ids, IP, User-Agent, email, URLs.
- The device id is a random UUID (`~/.decompute/device.id`, `dev-<hex>`); only its sha256 is stored, so
  a device's weekly rows de-dupe without being re-identifiable. The same id joins downloads to savings.
- Website visits derive a **cookieless, daily-rotating, forward-secret** visitor hash from ip+ua+salt,
  then drop ip/ua. The per-day salt auto-expires, so past hashes can't be brute-forced back to ip/ua.
- The real guarantee is **allowlist binding**: each handler writes only a fixed set of scalar columns.
  A forbidden-key/non-scalar guard rejects content-shaped payloads as defense-in-depth.
- **Edge logging is an infra control:** Cloudflare can log IP/UA at the edge independently of the Worker.
  The "we never log IP/UA" promise requires Logpush / HTTP-request-logs / observability for these routes
  to be OFF or IP/UA-redacted.

## Reading the numbers (internal only)

`GET /v1/stats` requires `Authorization: Bearer <STATS_TOKEN>`. It returns savings + downloads (real vs
CI) + visits in one JSON. For ad-hoc analysis, run `infra/telemetry-worker/queries.sql` against D1.

**Label honestly** (see the caveat block atop `queries.sql`): distinct devices = `reporting_installs`
(devices that beaconed), **NOT users**. Counts are directional — a wipe/reinstall overcounts, a cloned
image undercounts, opt-outs are invisible. Never call it MAU/DAU or divide revenue by it.

## Opt-out

`DO_NOT_TRACK=1`, `DECOMPUTE_NO_TELEMETRY=1` (installer), `DECOMPUTE_TELEMETRY=off`, or
`decompute telemetry off`. The marketing-site beacon sends nothing under DNT or GPC.

## Deploy (worker)

See `infra/telemetry-worker/` — `wrangler.toml` (D1 + KV + `SITE_ORIGIN`), `schema.sql`, and the secrets
`STATS_TOKEN` + `VISIT_SALT_SECRET` (set via `wrangler secret put`).
