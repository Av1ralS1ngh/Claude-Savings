# Decompute Pro — purchase website

Two static pages that drive the Pro purchase + activation, **hardened**: they store nothing and
never handle raw license keys or device fingerprints. The gateway is the only store and keeps
**hashes only** (this replaces the pack's Google-Sheets ledger, which stored raw keys + raw MACs).

- `index.html` — plan + email → `POST {gateway}/v1/billing/checkout` → redirect to Stripe Checkout.
- `success.html` — Stripe returns here with `?session_id=…` → `POST {gateway}/v1/billing/redeem-checkout`
  → shows the license key **once**. The user activates it in the app (`decompute activate <key>`),
  which binds it to the Mac — the website never sees the device fingerprint.

## Why this is the secure shape

| Concern | This site | Pack's Sheets ledger |
|---|---|---|
| License keys at rest | only HMAC hash (in the gateway DB) | **raw key in a Sheet** |
| Device identity at rest | only HMAC hash, set by the app on activate | **raw MAC in a Sheet** |
| Single-redeem | enforced by `UNIQUE(stripe_checkout_session_id)` in the gateway | manual |

## Setup

1. Configure Stripe on the **gateway** (not here): `DECOMPUTE_STRIPE_SECRET_KEY`,
   `DECOMPUTE_STRIPE_WEBHOOK_SECRET`, `DECOMPUTE_STRIPE_PRICE_ID`, and
   `DECOMPUTE_BILLING_SUCCESS_URL=https://<this-site>/pro/success.html?session_id={CHECKOUT_SESSION_ID}`.
2. Point the pages at the gateway: they default to `http://127.0.0.1:8080`; override per-load with
   `?gw=https://your-gateway`. Edit the `GATEWAY` const for production.
3. Allow the site's origin on the gateway: add it to `DECOMPUTE_CORS_ORIGINS` (the pages send an
   `x-decompute-csrf` header, which satisfies the gateway's browser-mutation guard).

## Local test

```bash
# serve the site
python3 -m http.server 5500 --directory website   # then open http://localhost:5500/pro/
# allow it on the gateway
export DECOMPUTE_CORS_ORIGINS="http://localhost:5500,http://127.0.0.1:8080"
```

Without a Stripe key the gateway returns 503 on checkout (the page shows the error) — that's
expected; wire a `sk_test_…` to exercise the full flow end to end.

## Google sign-in

Intentionally omitted from this hardened version: Stripe Checkout already captures the buyer's
email, so a separate Google OAuth step (and its session/secret surface) isn't needed for the
purchase. Add it later only if you want a self-serve account portal for license recovery.
