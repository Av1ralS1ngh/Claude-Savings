/**
 * Decompute license Worker — the server-side half of Pro licensing.
 *
 *   POST /checkout         create a Stripe Checkout Session, return {url}     (website "Buy Now")
 *   POST /webhook          Stripe webhook → sign a Pro license → store in KV  (server-to-server)
 *   GET  /license?device=  return this device's signed license, if paid       (extension "Activate Pro")
 *
 * Secrets (wrangler secret put …):
 *   STRIPE_SECRET_KEY        rk_live_… (Checkout Sessions write)
 *   STRIPE_WEBHOOK_SECRET    whsec_…   (verify webhook authenticity)
 *   LICENSE_PRIVATE_KEY      the Ed25519 PKCS8 PEM whose PUBLIC half is embedded in the engine
 * KV binding:
 *   LICENSES                 wrangler kv namespace create LICENSES  → bind in wrangler.toml
 *
 * The license is bound to canonical_device_id = sha256(raw device id). The buyer's raw device id
 * rides on the checkout as client_reference_id (the extension appends it to the Payment Link), so the
 * webhook can mint a license for exactly that machine. Format matches decompute_gateway/signed_license.py.
 */

const PRICE_ID = "price_1Te0pKFbGwqVxzy7wyp1yQ1d"; // Pro plan ($15/mo subscription)
const SUCCESS_URL = "https://decompute.run/pro/success";
const CANCEL_URL = "https://decompute.run/pro";
const KID = "1bb67b036447838e"; // must match an EMBEDDED_LICENSE_PUBLIC_KEYS entry in the engine
const LICENSE_DAYS = 35; // a payment grants this many days — > the ~30-day cycle, so renewals + the
                         // extension's auto-refresh keep an offline license fresh with a few days' grace.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;
    if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
    if (request.method === "GET" && pathname === "/") return cors(json({ ok: true, service: "decompute-license" }));
    if (request.method === "POST" && pathname === "/checkout") return cors(await handleCheckout(request, env));
    if (request.method === "POST" && pathname === "/webhook") return await handleWebhook(request, env); // no CORS (Stripe → us)
    if (request.method === "GET" && pathname === "/license") return cors(await handleLicense(url, env));
    return cors(json({ error: "not_found" }, 404));
  },
};

// ── POST /checkout ────────────────────────────────────────────────────────────
async function handleCheckout(request, env) {
  let body = {};
  try { body = await request.json(); } catch { /* empty body ok */ }
  const form = new URLSearchParams();
  form.set("mode", "subscription");
  form.set("line_items[0][price]", PRICE_ID);
  form.set("line_items[0][quantity]", "1");
  form.set("success_url", `${SUCCESS_URL}?session_id={CHECKOUT_SESSION_ID}`);
  form.set("cancel_url", CANCEL_URL);
  if (body.device) form.set("client_reference_id", String(body.device).slice(0, 200));
  if (body.email) form.set("customer_email", String(body.email).slice(0, 320));
  const r = await fetch("https://api.stripe.com/v1/checkout/sessions", {
    method: "POST",
    headers: { authorization: `Bearer ${env.STRIPE_SECRET_KEY}`, "content-type": "application/x-www-form-urlencoded" },
    body: form,
  });
  const data = await r.json();
  if (!r.ok) return json({ error: data?.error?.message || "stripe_error" }, 502);
  return json({ url: data.url });
}

// ── POST /webhook ───────────────────────────────────────────────────────────────
async function handleWebhook(request, env) {
  const raw = await request.text();
  const sig = request.headers.get("stripe-signature") || "";
  if (!(await verifyStripeSig(raw, sig, env.STRIPE_WEBHOOK_SECRET))) return json({ error: "bad_signature" }, 400);
  let event;
  try { event = JSON.parse(raw); } catch { return json({ error: "bad_json" }, 400); }

  const obj = event.data?.object || {};
  if (event.type === "checkout.session.completed") {
    const device = obj.client_reference_id;
    const email = obj.customer_details?.email || obj.customer_email || null;
    if (device && obj.payment_status !== "unpaid") {
      await issueLicense(env, device, email);
      // Map subscription → device so RENEWAL invoices (which carry no client_reference_id) can re-issue.
      if (obj.subscription) await env.LICENSES.put(`sub:${obj.subscription}`, device, { expirationTtl: 400 * 86400 });
    }
  } else if (event.type === "invoice.paid" || event.type === "invoice.payment_succeeded") {
    // Subscription renewal: extend the license for the device tied to this subscription, so a paying
    // customer never lapses. (Requires the Stripe webhook to ALSO send invoice.paid — see README.)
    if (obj.subscription) {
      const device = await env.LICENSES.get(`sub:${obj.subscription}`);
      if (device) await issueLicense(env, device, obj.customer_email || null);
    }
  }
  return json({ received: true });
}

async function issueLicense(env, rawDevice, email) {
  const deviceId = await canonicalDeviceId(rawDevice);
  const now = new Date();
  const expires = new Date(now.getTime() + LICENSE_DAYS * 86400 * 1000);
  const license = await signLicense({
    privatePem: env.LICENSE_PRIVATE_KEY,
    kid: KID,
    licenseId: "lic_" + deviceId.slice(0, 24),
    deviceId,
    issuedAt: now,
    expiresAt: expires,
    tier: "pro",
    subject: email || undefined,
  });
  // Keyed by the RAW device id (what the extension queries with); auto-expire a week after the license.
  await env.LICENSES.put(`lic:${rawDevice}`, license, { expirationTtl: (LICENSE_DAYS + 7) * 86400 });
}

// ── GET /license?device= ───────────────────────────────────────────────────────
async function handleLicense(url, env) {
  const device = url.searchParams.get("device");
  if (!device) return json({ error: "device required" }, 400);
  const license = await env.LICENSES.get(`lic:${device}`);
  if (!license) return json({ error: "not_found" }, 404);
  return json({ license });
}

// ── signed-license minting (matches signed_license.py byte-for-byte on the wire) ─
async function signLicense({ privatePem, kid, licenseId, deviceId, issuedAt, expiresAt, tier, subject }) {
  const payload = {
    v: 1,
    license_id: licenseId,
    device_id: deviceId,
    tier,
    issued_at: iso(issuedAt),
    expires_at: expiresAt ? iso(expiresAt) : null,
    max_devices: 1,
  };
  if (subject) payload.subject = subject;
  // The verifier checks the signature over these EXACT bytes (it never re-serializes), so key order
  // here is irrelevant — only that we sign and transmit the identical payload bytes.
  const payloadBytes = utf8(JSON.stringify(payload));
  const key = await crypto.subtle.importKey("pkcs8", pemToDer(privatePem), { name: "Ed25519" }, false, ["sign"]);
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, key, payloadBytes);
  const envelope = { v: 1, kid, payload_b64: b64url(payloadBytes), sig_b64: b64url(sig) };
  return b64url(utf8(JSON.stringify(envelope)));
}

async function canonicalDeviceId(raw) {
  const digest = await crypto.subtle.digest("SHA-256", utf8(String(raw).trim()));
  return hex(new Uint8Array(digest));
}

// ── Stripe webhook signature (t.payload HMAC-SHA256, constant-time compare) ──────
async function verifyStripeSig(payload, header, secret) {
  if (!secret || !header) return false;
  const parts = Object.fromEntries(header.split(",").map((kv) => kv.split("=")));
  if (!parts.t || !parts.v1) return false;
  const key = await crypto.subtle.importKey("raw", utf8(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, utf8(`${parts.t}.${payload}`));
  return timingSafeEqual(hex(new Uint8Array(mac)), parts.v1);
}

// ── small helpers ───────────────────────────────────────────────────────────────
function utf8(s) { return new TextEncoder().encode(s); }
function hex(bytes) { return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join(""); }
function iso(d) { return d.toISOString().replace(/\.\d{3}Z$/, "Z"); }

function b64url(bytes) {
  let bin = "";
  const arr = new Uint8Array(bytes);
  for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToDer(pem) {
  const b64 = pem
    .replace(/\\r/g, "").replace(/\\n/g, "\n") // tolerate dashboards that store newlines as literal "\n"
    .replace(/-----BEGIN [^-]+-----/, "")
    .replace(/-----END [^-]+-----/, "")
    .replace(/\s+/g, "");
  const bin = atob(b64);
  const der = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) der[i] = bin.charCodeAt(i);
  return der.buffer;
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
function cors(resp) {
  resp.headers.set("access-control-allow-origin", "*");
  resp.headers.set("access-control-allow-methods", "GET, POST, OPTIONS");
  resp.headers.set("access-control-allow-headers", "content-type");
  return resp;
}
