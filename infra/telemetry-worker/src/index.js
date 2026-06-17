/**
 * Decompute telemetry Worker — ANONYMOUS, COUNTS-ONLY telemetry.
 *
 *   POST /v1/savings          gateway weekly savings rollup (counts only)        [unchanged behavior]
 *   POST /v1/event            install beacon from scripts/install.sh             [new; server-to-server]
 *   POST /v1/download         alias of /v1/event                                 [new]
 *   POST /v1/visit            marketing-site visit beacon (browser, CORS, DNT)   [new]
 *   GET  /v1/stats            INTERNAL aggregate dashboard — Bearer-gated         [now token-gated]
 *   GET  /                    health
 *
 * PRIVACY CONTRACT (sacred — do not weaken):
 *   - Store ONLY counts + coarse os/version + hashes. NEVER prompts/files/content/IP/UA/email/raw-ids.
 *   - The device id is a random UUID minted client-side; we store ONLY its sha256.
 *   - /v1/visit reads cf-connecting-ip + user-agent ONLY to derive a cookieless, DAILY-ROTATING,
 *     FORWARD-SECRET visitor hash, then drops them. There is NO ip/ua/referrer/url column anywhere.
 *   - The REAL guarantee is that every handler binds ONLY a fixed allowlist of scalar fields into SQL;
 *     the forbidden-key/non-scalar guard is defense-in-depth on top of that.
 *   - EDGE LOGGING IS AN INFRA CONTROL, NOT CODE: Cloudflare can log cf-connecting-ip / User-Agent at
 *     the edge independently of this Worker. The "IP/UA never logged" guarantee REQUIRES that Logpush /
 *     HTTP-request-logs / Workers observability for these routes be OFF or IP/UA-redacted. See README.
 *
 * Bindings:
 *   env.DB                 D1 (binding "DB")
 *   env.STATS_TOKEN        secret — Bearer token gating GET /v1/stats
 *   env.SITE_ORIGIN        var/secret — exact marketing origin for /v1/visit CORS (no wildcard)
 *   env.VISIT_SALT_SECRET  secret — long-lived HMAC key for the visitor hash
 *   env.VISIT_SALT_KV      KV namespace — holds the EPHEMERAL per-UTC-day salt (auto-expires → forward secrecy)
 */

const MAX_BODY = 4 * 1024;       // savings/event bodies are tiny
const VISIT_MAX_BODY = 1024;     // {path,nonce} is tinier
const STATS_CACHE_S = 300;       // synthetic-key, PRIVATE cache window for the internal dashboard
const VISIT_PATH_ALLOW = new Set(["/", "/pro", "/pricing", "/docs", "/download"]);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const { pathname } = url;

    // ── CORS preflight: ONLY the browser-facing /v1/visit gets a grant ──
    if (request.method === "OPTIONS") {
      if (pathname === "/v1/visit") return corsForVisit(request, env, new Response(null, { status: 204 }));
      return new Response(null, { status: 204 }); // bare preflight, no CORS grant
    }

    if (request.method === "GET" && pathname === "/") {
      return json({ ok: true, service: "decompute-telemetry" }); // no CORS
    }
    if (request.method === "POST" && pathname === "/v1/savings") {
      return await handleSavings(request, env); // server-to-server (gateway): no CORS
    }
    if (request.method === "POST" && (pathname === "/v1/event" || pathname === "/v1/download")) {
      if (await rateLimited(request, env, "event")) return json({ error: "rate_limited" }, 429);
      return await handleEvent(request, env); // server-to-server (install.sh): no CORS
    }
    if (request.method === "POST" && pathname === "/v1/visit") {
      // Rate-limit BEFORE the handler; still return 204 (browser path leaks nothing, even when throttled).
      if (await rateLimited(request, env, "visit")) return corsForVisit(request, env, new Response(null, { status: 204 }));
      return corsForVisit(request, env, await handleVisit(request, env)); // browser: scoped CORS
    }
    if (request.method === "GET" && pathname === "/v1/stats") {
      return await handleStats(request, env, ctx); // token-gated INTERNAL: no CORS
    }
    return json({ error: "not_found" }, 404);
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// POST /v1/savings  — UNCHANGED behavior. (device normalized below so its hash
// is byte-identical to /v1/event's device_hash, guaranteeing the join.)
// ─────────────────────────────────────────────────────────────────────────────
async function handleSavings(request, env) {
  const body = await readJsonCapped(request, MAX_BODY);
  if (body === SIZE_ERR) return json({ error: "payload_too_large" }, 413);
  if (body === PARSE_ERR) return json({ error: "bad_json" }, 400);
  if (!body || typeof body !== "object") return json({ error: "bad_body" }, 400);
  if (hasForbiddenOrNonScalar(body)) return json({ error: "rejected_non_count_field" }, 422);

  const device = normDeviceId(body.device);
  if (!device) return json({ error: "device_required" }, 400);
  const week = normWeek(body.week);
  if (!week) return json({ error: "week_required" }, 400);

  const requests = clampInt(body.requests);
  const tokensSaved = clampInt(body.tokens_saved);
  const tokensBefore = clampInt(body.tokens_before);
  const savingsUsd = clampFloat(body.est_savings_usd);
  const os = normOs(body.os);
  const version = normVersion(body.version);

  const deviceHash = await sha256Hex(device);

  await env.DB.prepare(
    `INSERT INTO savings (device_hash, week, requests, tokens_saved, tokens_before, est_savings_usd, os, version, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
     ON CONFLICT(device_hash, week) DO UPDATE SET
       requests=excluded.requests, tokens_saved=excluded.tokens_saved, tokens_before=excluded.tokens_before,
       est_savings_usd=excluded.est_savings_usd, os=excluded.os, version=excluded.version, updated_at=excluded.updated_at`
  ).bind(deviceHash, week, requests, tokensSaved, tokensBefore, savingsUsd, os, version, Date.now()).run();

  return json({ ok: true });
}

// ─────────────────────────────────────────────────────────────────────────────
// POST /v1/event (alias /v1/download) — ANONYMOUS install beacon from install.sh.
// Counts only: sha256(device.id) [JOINs to savings], coarse os, ci flag, version.
// Idempotent on (device_hash, day, ci): same-day re-runs UPSERT one row → no inflation.
// UNAUTHENTICATED by design (a token in a public curl|bash script is world-readable,
// so it authenticates nothing). Volumetric abuse is bounded at the Cloudflare edge
// (WAF/Rate-Limiting on this path) — the Worker never reads the IP.
// ─────────────────────────────────────────────────────────────────────────────
async function handleEvent(request, env) {
  const body = await readJsonCapped(request, MAX_BODY);
  if (body === SIZE_ERR) return json({ error: "payload_too_large" }, 413);
  if (body === PARSE_ERR) return json({ error: "bad_json" }, 400);
  if (!body || typeof body !== "object") return json({ error: "bad_body" }, 400);
  if (hasForbiddenOrNonScalar(body)) return json({ error: "rejected_non_count_field" }, 422);

  // install_id MUST be the dev-... id from ~/.decompute/device.id (accept legacy "device" too),
  // normalized to the EXACT shape savings hashes, so device_hash here == savings.device_hash.
  const device = normDeviceId(body.install_id || body.device);
  if (!device) return json({ error: "install_id_required" }, 400);

  const os = normOs(body.os);
  const ci = body.ci === true ? 1 : 0;        // strict: only an explicit boolean true is CI
  const version = normVersion(body.version);

  const deviceHash = await sha256Hex(device);
  const day = utcDay();                         // server-computed UTC "YYYY-MM-DD"
  const week = isoWeekFromDate(new Date());     // server-computed "YYYY-Www" (matches telemetry.py)

  // PK (device_hash, day, ci) — same-day re-install is a no-op for counts; a real install and a
  // CI install from the same machine are two honest, separable rows (ci is in the PK).
  await env.DB.prepare(
    `INSERT INTO downloads (device_hash, day, week, ci, os, version, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
     ON CONFLICT(device_hash, day, ci) DO UPDATE SET
       week=excluded.week, os=excluded.os, version=excluded.version, updated_at=excluded.updated_at`
  ).bind(deviceHash, day, week, ci, os, version, Date.now()).run();

  return json({ ok: true });
}

// ─────────────────────────────────────────────────────────────────────────────
// POST /v1/visit — the ONLY browser-facing, secretless path.
// Privacy-maximal: NOTHING on DNT/GPC; ip+ua used ONLY to derive a cookieless,
// DAILY-ROTATING, FORWARD-SECRET visitor hash, then dropped. Stores ONLY:
// visitor_hash, day, week, allowlisted path, sha256(nonce). No ip/ua/url column exists.
// Always returns 204 (leaks nothing). Dedupe via PK + INSERT OR IGNORE.
// ─────────────────────────────────────────────────────────────────────────────
async function handleVisit(request, env) {
  // SERVER-SIDE DNT / GPC (belt-and-suspenders behind the client check) → record NOTHING.
  if (request.headers.get("dnt") === "1" || request.headers.get("sec-gpc") === "1") {
    return new Response(null, { status: 204 });
  }

  const body = await readJsonCapped(request, VISIT_MAX_BODY);
  // Never leak detail to the browser: any problem → silent 204, no write.
  if (body === SIZE_ERR || body === PARSE_ERR || !body || typeof body !== "object") {
    return new Response(null, { status: 204 });
  }
  if (hasForbiddenOrNonScalar(body)) return new Response(null, { status: 204 });

  const path = normVisitPath(body.path); // → allowlisted path, "/other", or null
  if (path === null) return new Response(null, { status: 204 });

  // nonce is dedupe-only and client-controlled → store its HASH, never the plaintext.
  const rawNonce = String(body.nonce || "").slice(0, 128);
  const nonceHash = await sha256Hex(rawNonce);

  const day = utcDay();
  const week = isoWeekFromDate(new Date());
  const ip = request.headers.get("cf-connecting-ip") || "";
  const ua = request.headers.get("user-agent") || "";

  // FORWARD-SECRET daily salt: HMAC(long-lived secret, dailyEphemeralSalt || day). The ephemeral
  // salt is random, stored in KV, and AUTO-EXPIRES after ~2 days — so once a day passes its salt is
  // GONE and that day's stored visitor hashes are NOT brute-forceable back to (ip,ua) even if the
  // long-lived secret later leaks. (This is the genuine forward secrecy a secret-only salt lacked.)
  const dailySalt = await dailyVisitSalt(env, day);
  if (!dailySalt) return new Response(null, { status: 204 }); // no salt source → fail closed, store nothing
  const visitorHash = await sha256Hex(dailySalt + "|" + ip + "|" + ua);
  // (ip, ua now go out of scope — nothing downstream can persist them.)

  try {
    await env.DB.prepare(
      `INSERT OR IGNORE INTO visits (visitor_hash, day, week, path, nonce, updated_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6)`
    ).bind(visitorHash, day, week, path, nonceHash, Date.now()).run();
  } catch (_) { /* never surface storage errors to the browser */ }

  return new Response(null, { status: 204 });
}

// Per-UTC-day ephemeral salt. Returns a hex string mixing a long-lived secret with a random,
// auto-expiring per-day value. KV is justified here SPECIFICALLY for forward secrecy: the random
// component is discarded after the day, which a pure secret-derived salt cannot achieve.
async function dailyVisitSalt(env, day) {
  const secret = env.VISIT_SALT_SECRET;
  if (!secret) return "";                       // no secret → fail closed (caller stores nothing)
  const kv = env.VISIT_SALT_KV;
  if (!kv) {
    // Degraded fallback (no KV bound): secret-derived only. Functions correctly but is NOT
    // forward-secret — document and prefer KV in production.
    return await hmacHex(secret, "v1|" + day);
  }
  const k = "visit-salt:" + day;
  let eph = await kv.get(k);
  if (!eph) {
    eph = randHex(32);
    // expirationTtl 172800s (~2 days) covers UTC-edge writers; after that the ephemeral salt is gone.
    try { await kv.put(k, eph, { expirationTtl: 172800 }); } catch (_) { /* best-effort */ }
    // Re-read to win races (another isolate may have written first); keep whichever persisted.
    const winner = await kv.get(k);
    if (winner) eph = winner;
  }
  return await hmacHex(secret, "v1|" + day + "|" + eph);
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /v1/stats — INTERNAL, Bearer-gated, leak-safe cache, dashboard join.
//   Auth FIRST (before any cache touch). Unauthorized → 401 empty body.
//   Missing secret → 503 fail-closed. Cache stored PRIVATE under a SYNTHETIC key
//   that no inbound URL can target; client response is always private, no-store.
// ─────────────────────────────────────────────────────────────────────────────
async function handleStats(request, env, ctx) {
  // (1) Auth is the VERY FIRST thing — an unauthenticated request can never reach a cache op.
  const authed = await authorizeStats(request, env);
  if (authed !== true) return authed; // 401 or 503 Response

  const cache = caches.default;
  // (2) MUST be synthetic, NOT request.url — a request-URL key + a cached body = token bypass.
  const cacheKey = new Request("https://cache.internal/v1/stats", { method: "GET" });

  try {
    const hit = await cache.match(cacheKey);
    if (hit) {
      const r = new Response(hit.body, hit);
      r.headers.set("cache-control", "private, no-store");
      return r;
    }
  } catch (_) { /* cache unavailable → fall through to a live query */ }

  const data = await computeStats(env);
  const res = json(data);

  // Stored copy is PRIVATE (not public) and keyed synthetically. Auth gates the read path, so this
  // copy can never be served to an unauthenticated caller.
  const toStore = res.clone();
  toStore.headers.set("cache-control", `private, max-age=${STATS_CACHE_S}`);
  try { ctx.waitUntil(cache.put(cacheKey, toStore)); } catch (_) { /* still return fresh */ }

  res.headers.set("cache-control", "private, no-store");
  return res;
}

// Aggregate read. savings fields UNCHANGED (backward compatible). New blocks are scalar subqueries
// so an empty downloads/visits table yields 0 (no JOIN fan-out). Distinct devices is labeled
// reporting_installs — NOT users (wipe → overcount, clone → undercount; we do not claim humans).
async function computeStats(env) {
  const row = await env.DB.prepare(
    `SELECT
       (SELECT COUNT(DISTINCT device_hash) FROM savings)        AS reporting_installs,
       (SELECT COALESCE(SUM(requests),0)        FROM savings)   AS requests,
       (SELECT COALESCE(SUM(tokens_saved),0)    FROM savings)   AS tokens_saved,
       (SELECT COALESCE(SUM(tokens_before),0)   FROM savings)   AS tokens_before,
       (SELECT COALESCE(SUM(est_savings_usd),0) FROM savings)   AS est_savings_usd,
       (SELECT COUNT(*)                          FROM downloads) AS downloads_total,
       (SELECT COUNT(*)         FROM downloads WHERE ci = 0)     AS downloads_real,
       (SELECT COUNT(*)         FROM downloads WHERE ci = 1)     AS downloads_ci,
       (SELECT COUNT(DISTINCT device_hash) FROM downloads WHERE ci = 0) AS downloads_devices,
       (SELECT COUNT(*)                          FROM visits)    AS visits_total,
       (SELECT COUNT(DISTINCT visitor_hash)      FROM visits)    AS visits_distinct_directional`
  ).first();

  const usd = Number(row?.est_savings_usd || 0);
  return {
    // Backward-compatible aliases: keep "devices" so any existing reader doesn't break,
    // but the AUTHORITATIVE name the company reads is reporting_installs (NOT users).
    devices: Number(row?.reporting_installs || 0),
    reporting_installs: Number(row?.reporting_installs || 0),
    requests: Number(row?.requests || 0),
    tokens_saved: Number(row?.tokens_saved || 0),
    tokens_before: Number(row?.tokens_before || 0),
    est_savings_usd: Math.round(usd * 100) / 100,
    downloads_total: Number(row?.downloads_total || 0),
    downloads_real: Number(row?.downloads_real || 0),      // ci = 0
    downloads_ci: Number(row?.downloads_ci || 0),          // ci = 1
    downloads_devices: Number(row?.downloads_devices || 0),
    visits_total: Number(row?.visits_total || 0),                          // pageviews (inflatable)
    visits_distinct_directional: Number(row?.visits_distinct_directional || 0), // NOT cross-day unique humans
  };
}

// ── auth: timing-safe Bearer check ───────────────────────────────────────────
// true if authorized, else a ready-to-return Response. Missing secret → 503 fail-closed.
// Wrong/missing token → 401 empty body + www-authenticate. SHA-256 both sides first so inputs are
// always 32 bytes (crypto.subtle.timingSafeEqual THROWS on unequal length) → no length/content leak.
async function authorizeStats(request, env) {
  const expected = env.STATS_TOKEN;
  if (!expected) return new Response(null, { status: 503 }); // never accidentally open

  const header = request.headers.get("authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(header);
  const presented = m ? m[1] : "";

  const ok = await timingSafeEqualStr(presented, expected);
  if (!ok) return new Response(null, { status: 401, headers: { "www-authenticate": "Bearer" } });
  return true;
}

async function timingSafeEqualStr(a, b) {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  // SHA-256 guarantees both buffers are 32 bytes (required by timingSafeEqual). NEVER fall back to ===.
  return crypto.subtle.timingSafeEqual(da, db);
}

// ── CORS: per-route, NEVER blanket. ONLY /v1/visit, ONLY the exact marketing origin. ─────────
// No wildcard. access-control-allow-credentials is NEVER set anywhere.
function corsForVisit(request, env, res) {
  const allowed = env.SITE_ORIGIN || "https://claude.decompute.run";
  const origin = request.headers.get("origin");
  if (origin && origin === allowed) {
    res.headers.set("access-control-allow-origin", allowed); // echo ONLY the exact allowed origin
  }
  res.headers.set("vary", "Origin");                         // don't cross-serve a CORS grant
  res.headers.set("access-control-allow-methods", "POST, OPTIONS");
  res.headers.set("access-control-allow-headers", "content-type");
  res.headers.set("access-control-max-age", "86400");
  return res;
}

// ── rate limiting (native binding) ────────────────────────────────────────────
// Per-IP throttle for the UNAUTHENTICATED ingest endpoints. The IP is used ONLY as the
// transient rate-limit key (passed to the binding) — it is NEVER stored or logged. Keyed by
// endpoint+IP so /v1/event and /v1/visit have independent budgets. Fail-OPEN: if the binding
// is absent (e.g. local dev / a deploy without it) we don't block — abuse control degrades
// gracefully rather than breaking ingestion. Returns true if the request should be rejected.
async function rateLimited(request, env, scope) {
  const rl = env.INGEST_RL;
  if (!rl || typeof rl.limit !== "function") return false; // no binding → don't block
  const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
  try {
    const { success } = await rl.limit({ key: scope + ":" + ip });
    return success === false;
  } catch (_) {
    return false; // limiter error → fail open, never drop telemetry on infra hiccups
  }
}

// ── input helpers ────────────────────────────────────────────────────────────
const SIZE_ERR = Symbol("size");
const PARSE_ERR = Symbol("parse");

// Read the body with a REAL byte cap on the actual read (Content-Length can be absent/chunked, so it
// is only a fast-path, never the sole bound). Returns parsed object, SIZE_ERR, or PARSE_ERR.
async function readJsonCapped(request, max) {
  const len = Number(request.headers.get("content-length") || 0);
  if (len > max) return SIZE_ERR; // fast reject when the header is present
  let text;
  try { text = await request.text(); } catch { return PARSE_ERR; }
  if (text.length > max) return SIZE_ERR; // enforce on the bytes we actually got
  if (!text) return PARSE_ERR;
  try { return JSON.parse(text); } catch { return PARSE_ERR; }
}

// HARD PRIVACY GUARD. The REAL guarantee is that handlers bind ONLY a fixed allowlist of scalar
// fields into SQL — this guard is defense-in-depth. It (a) rejects content-shaped keys and (b)
// rejects ANY non-scalar value, so nested content {"extra":{"prompt":...}} can't smuggle through.
const FORBIDDEN_KEYS = new Set([
  "prompt", "prompts", "message", "messages", "content", "text", "file", "files", "input", "system",
  "body", "ip", "ua", "user_agent", "useragent", "email", "url", "href", "referrer", "query",
]);
function hasForbiddenOrNonScalar(body) {
  for (const k of Object.keys(body)) {
    if (FORBIDDEN_KEYS.has(k.toLowerCase())) return true;
    const v = body[k];
    if (v !== null && typeof v === "object") return true; // no objects/arrays → no nested smuggling
  }
  return false;
}

function clampInt(v) { const n = Math.floor(Number(v)); return Number.isFinite(n) && n > 0 ? n : 0; }
function clampFloat(v) { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : 0; }

// Device/install id normalized to the EXACT bytes savings hashes: trimmed, lowercased, and validated
// to ^dev-[0-9a-f]{32}$ (the telemetry.py / install.sh format). Anything else → "" (rejected upstream).
// This guarantees download.device_hash == savings.device_hash (joinable) and blocks unbounded-id inflation.
function normDeviceId(v) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  return /^dev-[0-9a-f]{32}$/.test(s) ? s : "";
}

// Coarse OS allowlist (sys.platform families). Anything else → "unknown" (never free-text).
function normOs(v) {
  const s = String(v || "").toLowerCase().slice(0, 16);
  if (s === "darwin") return "darwin";
  if (s === "linux") return "linux";
  if (s === "win32" || s === "windows" || s === "cygwin" || s === "msys") return "win32";
  return "unknown";
}

// Version clamped to a strict charset so it can't carry arbitrary text / pseudo-PII.
function normVersion(v) {
  const s = String(v || "").slice(0, 32);
  return /^[0-9A-Za-z.+_-]*$/.test(s) ? s : "";
}

/** ISO week "YYYY-Www" (or "" if bad). */
function normWeek(w) {
  const s = String(w || "");
  return /^\d{4}-W\d{2}$/.test(s) ? s : "";
}

// Normalize a visit path → allowlisted path, "/other" (clean-but-unknown), or null (hostile → drop).
function normVisitPath(raw) {
  let s = String(raw == null ? "" : raw).trim().toLowerCase();
  if (!s) return null;
  if (s.length > 64) return null;                 // overlong
  if (s.includes("://")) return null;             // full URL
  if (s.includes("@")) return null;               // email
  if (s.includes("%")) return null;               // percent-encoding (no decoding games)
  if (s.includes("?") || s.includes("#")) return null; // query/hash must be stripped client-side
  if (s.includes("\\") || s.includes("..")) return null; // traversal
  if (!s.startsWith("/")) return null;
  if (!/^\/[a-z0-9/_-]*$/.test(s)) return null;   // strict charset
  if (s.length > 1 && s.endsWith("/")) s = s.slice(0, -1); // "/pro/" → "/pro"
  return VISIT_PATH_ALLOW.has(s) ? s : "/other";
}

// ── crypto / time helpers ─────────────────────────────────────────────────────
async function sha256Hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function hmacHex(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret || ""),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function randHex(nBytes) {
  const a = new Uint8Array(nBytes);
  crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function utcDay(d = new Date()) {
  return d.getUTCFullYear() + "-" +
    String(d.getUTCMonth() + 1).padStart(2, "0") + "-" +
    String(d.getUTCDate()).padStart(2, "0");
}
// ISO-8601 week "YYYY-Www" computed from a UTC date — matches telemetry.py's isocalendar() output,
// so downloads.week / visits.week align with savings.week.
function isoWeekFromDate(date = new Date()) {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const dayNum = d.getUTCDay() === 0 ? 7 : d.getUTCDay(); // Mon=1..Sun=7
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);              // nearest Thursday
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
  return d.getUTCFullYear() + "-W" + String(week).padStart(2, "0");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
