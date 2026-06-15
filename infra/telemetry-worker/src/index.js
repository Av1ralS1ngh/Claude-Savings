/**
 * Decompute telemetry Worker — collects ANONYMOUS, COUNTS-ONLY savings totals.
 *
 *   POST /v1/savings   a device's weekly savings rollup (counts only) -> append to D1
 *   GET  /v1/stats     public aggregate totals (powers a "X tokens saved" number)     [cached]
 *   GET  /             health
 *
 * PRIVACY CONTRACT (the whole point — keep this exact):
 *   - We store ONLY counts: requests, tokens_saved, tokens_before, est_savings_usd, plus coarse
 *     os/version for support. NEVER prompts, files, messages, model inputs, or any user content.
 *   - The device id is a random UUID minted client-side (NO hardware info). We store only its
 *     sha256 — never the raw id — so rows can be de-duplicated per device-week without being
 *     re-identifiable.
 *   - We do NOT store the client IP (Cloudflare gives us cf headers; we ignore them).
 *   - A row is keyed (device_hash, week) so a re-send of the same week REPLACES, never double-counts.
 *
 * D1 binding:  DB   (wrangler d1 create decompute-telemetry  -> bind in wrangler.toml)
 * Schema:      see schema.sql (wrangler d1 execute decompute-telemetry --file schema.sql)
 */

const MAX_BODY = 4 * 1024; // a rollup is tiny; reject anything larger (defense-in-depth)
const STATS_CACHE_S = 300; // /v1/stats is a heavy SUM; cache 5 min at the edge

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const { pathname } = url;
    if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
    if (request.method === "GET" && pathname === "/") return cors(json({ ok: true, service: "decompute-telemetry" }));
    if (request.method === "POST" && pathname === "/v1/savings") return cors(await handleSavings(request, env));
    if (request.method === "GET" && pathname === "/v1/stats") return cors(await handleStats(env, ctx));
    return cors(json({ error: "not_found" }, 404));
  },
};

// ── POST /v1/savings ────────────────────────────────────────────────────────────
async function handleSavings(request, env) {
  // Reject oversized bodies before reading them fully.
  const len = Number(request.headers.get("content-length") || 0);
  if (len > MAX_BODY) return json({ error: "payload_too_large" }, 413);

  let body;
  try { body = await request.json(); } catch { return json({ error: "bad_json" }, 400); }
  if (!body || typeof body !== "object") return json({ error: "bad_body" }, 400);

  // HARD PRIVACY GUARD: reject anything that smells like user content. We only ever accept counts.
  // If a future client bug tried to send a prompt/message/file field, we drop the whole request
  // rather than risk storing content. This is belt-and-suspenders on top of the client-side filter.
  const FORBIDDEN = ["prompt", "prompts", "message", "messages", "content", "text", "file", "files", "input", "system", "body"];
  for (const k of Object.keys(body)) {
    if (FORBIDDEN.includes(k.toLowerCase())) return json({ error: "rejected_non_count_field" }, 422);
  }

  const device = String(body.device || "").slice(0, 200);
  if (!device) return json({ error: "device_required" }, 400);
  const week = normWeek(body.week);
  if (!week) return json({ error: "week_required" }, 400);

  // Coerce every metric to a non-negative number — never trust the client's types.
  const requests   = clampInt(body.requests);
  const tokensSaved = clampInt(body.tokens_saved);
  const tokensBefore = clampInt(body.tokens_before);
  const savingsUsd = clampFloat(body.est_savings_usd);
  const os = String(body.os || "").slice(0, 32);
  const version = String(body.version || "").slice(0, 32);

  const deviceHash = await sha256Hex(device);

  // Upsert keyed on (device_hash, week): a re-send of the same week REPLACES, so a device that
  // beacons twice in a week (restart) can't inflate totals.
  await env.DB.prepare(
    `INSERT INTO savings (device_hash, week, requests, tokens_saved, tokens_before, est_savings_usd, os, version, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
     ON CONFLICT(device_hash, week) DO UPDATE SET
       requests=excluded.requests, tokens_saved=excluded.tokens_saved, tokens_before=excluded.tokens_before,
       est_savings_usd=excluded.est_savings_usd, os=excluded.os, version=excluded.version, updated_at=excluded.updated_at`
  ).bind(deviceHash, week, requests, tokensSaved, tokensBefore, savingsUsd, os, version, Date.now()).run();

  return json({ ok: true });
}

// ── GET /v1/stats ─────────────────────────────────────────────────────────────
// Public aggregate totals — safe to expose (no per-device rows, just sums). Edge-cached.
async function handleStats(env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request("https://telemetry.decompute.run/v1/stats");
  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const row = await env.DB.prepare(
    `SELECT COUNT(DISTINCT device_hash) AS devices,
            COALESCE(SUM(requests),0)        AS requests,
            COALESCE(SUM(tokens_saved),0)    AS tokens_saved,
            COALESCE(SUM(tokens_before),0)   AS tokens_before,
            COALESCE(SUM(est_savings_usd),0) AS est_savings_usd
     FROM savings`
  ).first();

  const res = json({
    devices: row?.devices || 0,
    requests: row?.requests || 0,
    tokens_saved: row?.tokens_saved || 0,
    tokens_before: row?.tokens_before || 0,
    est_savings_usd: Math.round((row?.est_savings_usd || 0) * 100) / 100,
  });
  res.headers.set("cache-control", `public, max-age=${STATS_CACHE_S}`);
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
}

// ── helpers ─────────────────────────────────────────────────────────────────────
function clampInt(v) { const n = Math.floor(Number(v)); return Number.isFinite(n) && n > 0 ? n : 0; }
function clampFloat(v) { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : 0; }

/** Accept an ISO week like "2026-W24" (or a YYYY-MM-DD we floor to its ISO week). Returns "" if bad. */
function normWeek(w) {
  const s = String(w || "");
  if (/^\d{4}-W\d{2}$/.test(s)) return s;
  return "";
}

async function sha256Hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

function cors(res) {
  res.headers.set("access-control-allow-origin", "*");
  res.headers.set("access-control-allow-methods", "GET, POST, OPTIONS");
  res.headers.set("access-control-allow-headers", "content-type");
  return res;
}
