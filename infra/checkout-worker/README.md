# Decompute License Worker

The server-side half of Pro licensing. One small Cloudflare Worker, three routes:

| Route | Who calls it | What it does |
|---|---|---|
| `POST /checkout` | website "Buy Now" | creates a Stripe Checkout Session, returns `{url}` |
| `POST /webhook` | Stripe | on payment, **signs a Pro license** bound to the buyer's device → stores in KV |
| `GET /license?device=<id>` | extension "Activate Pro" | returns that device's signed license once it's paid |

The Stripe secret and the Ed25519 license key live **only** here (as Wrangler secrets) — never on a
user's machine, the extension, or the website. The license format matches
`decompute_gateway/signed_license.py` and has been verified to pass the gateway's verifier.

## How the loop works

1. Extension "Upgrade to Pro" opens the Stripe Payment Link with `?client_reference_id=<deviceId>`.
2. Buyer pays. Stripe fires `checkout.session.completed` to `/webhook` with that device id.
3. The Worker mints an Ed25519-signed Pro license for `sha256(deviceId)` and stores it in KV.
4. Extension "Activate Pro" calls `/license?device=<deviceId>`, writes the result to
   `~/.decompute/license.dcmp`, and the gateway verifies it offline → Pro.

## Deploy

```bash
cd infra/checkout-worker
npm i -g wrangler
wrangler login

# 1) KV store for issued licenses — paste the printed id into wrangler.toml (id = "…")
wrangler kv namespace create LICENSES

# 2) Three secrets
wrangler secret put STRIPE_SECRET_KEY        # rk_live_… (Checkout Sessions write)
wrangler secret put STRIPE_WEBHOOK_SECRET    # whsec_…  (from the Stripe webhook you create below)
wrangler secret put LICENSE_PRIVATE_KEY       # paste the full PEM: ~/decompute-signing/license_private.pem

# 3) Ship it
wrangler deploy                               # → https://decompute-checkout.<account>.workers.dev
```

**Create the Stripe webhook** (gives you the `whsec_…`): Stripe → Developers → Webhooks → Add endpoint
→ URL `https://decompute-checkout.<account>.workers.dev/webhook`, events **`checkout.session.completed`**
**and `invoice.paid`** (the second one re-issues the license on each subscription renewal so paying
customers never lapse — without it, Pro ends after ~35 days).
→ copy the signing secret into `wrangler secret put STRIPE_WEBHOOK_SECRET`.

## Wire the extension to it

Set `decompute.licenseApiUrl` to the deployed URL (or tell me and I'll bake it into the build):

```
"decompute.licenseApiUrl": "https://decompute-checkout.<account>.workers.dev"
```

The Payment Link is already wired into `Upgrade to Pro`; override it with `decompute.checkoutUrl` if needed.

## Test the full loop

```bash
# 1) license endpoint is empty for a fresh device
curl "https://decompute-checkout.<account>.workers.dev/license?device=dev-test123"   # → 404 not_found

# 2) simulate a paid webhook (or just pay through the Payment Link with ?client_reference_id=dev-test123)
stripe trigger checkout.session.completed   # if using Stripe CLI; or real payment

# 3) the license now exists and the gateway accepts it
curl "https://decompute-checkout.<account>.workers.dev/license?device=dev-test123"   # → {"license":"…"}
```

The signing itself is already proven against the gateway verifier (see the repo's validation:
a Worker-signed license returns `valid=True, tier=pro`, and a different device is rejected `wrong_device`).
