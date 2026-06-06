# Stripe webhook → entitlement loop (setup & verification)

The webhook is the **only** path that grants/revokes a paid tier. Checkout works, but without a
configured + reachable webhook a customer can pay and never get Pro. This is how to wire and
verify it.

## What is already done (in-repo, no Stripe account needed)
- Route: `POST /v1/billing/stripe-webhook` (and alias `/v1/billing/webhook`), signature-verified,
  no auth. Unset secret → every event is `400`ed (fail-closed).
- Config: `DECOMPUTE_STRIPE_WEBHOOK_SECRET` (`config.py`). A **production** boot now refuses to
  start if billing is enabled but this secret is missing (`validate_for_startup`).
- Proof the loop works end-to-end (no real Stripe): `tests/test_billing_webhook_e2e.py` drives
  signed `checkout.session.completed` → tier **pro** → idempotent replay → `customer.subscription.deleted`
  → **free**, through the real HTTP routes with the authoritative re-fetch stubbed.
- Proof the live wire verifies signatures: `scripts/billing_webhook_smoke.py` against a running
  gateway (forged → 400, missing → 400, valid → 200, no Stripe contacted, no tier changed).

## Dev: get a real signing secret with the Stripe CLI
```bash
stripe login                                   # needs a key that authorizes the CLI
stripe listen --forward-to localhost:8080/v1/billing/stripe-webhook
# copy the printed whsec_... into .env:
#   DECOMPUTE_STRIPE_WEBHOOK_SECRET=whsec_...
./scripts/run_gateway.sh                        # RESTART — --reload does NOT re-source .env
# in another shell, drive the loop:
stripe trigger checkout.session.completed
stripe trigger customer.subscription.deleted
# watch the tier flip:
curl -s -H "x-api-key: $DECOMPUTE_API_KEY" localhost:8080/v1/billing/status | jq .tier
```
Constraint: with only a **live restricted** key (no test key), `stripe trigger` emits **test-mode**
objects whose ids won't resolve under a live-keyed gateway, so the grant won't complete. To
exercise grant end-to-end against real Stripe you need either a **test key** (test-mode listen +
trigger) or a real test purchase. The grant *logic* is already proven by the e2e test above.

## Prod: register a webhook endpoint
1. Stripe Dashboard → Developers → Webhooks → Add endpoint:
   `https://<your-host>/v1/billing/stripe-webhook`
2. Select events: `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`.
3. Copy the endpoint's **Signing secret** (`whsec_...`) into the prod env as
   `DECOMPUTE_STRIPE_WEBHOOK_SECRET`, alongside the live key/price/tier and the prod-gate vars
   (auth, Postgres, non-default salts).
4. Restart. Send a test event from the dashboard; confirm via `GET /v1/billing/status` and the
   `billing.*` audit log.

## Re-verify any time
```bash
# in-process loop (no Stripe):
pytest tests/test_billing_webhook_e2e.py tests/test_billing.py
# live signature gate (running gateway, no Stripe, no tier change):
set -a; source ./.env; set +a; python scripts/billing_webhook_smoke.py
```

> The current `.env` holds a **local** test secret (`whsec_local_…`) so the offline smoke passes.
> It is NOT a real Stripe secret — replace it with the `whsec_` from `stripe listen` (dev) or the
> dashboard endpoint (prod) before real events can be verified.
