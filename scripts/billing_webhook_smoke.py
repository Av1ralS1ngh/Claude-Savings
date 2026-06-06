#!/usr/bin/env python3
"""Offline smoke for the Stripe webhook signature gate on a RUNNING gateway.

Proves the live wire WITHOUT contacting Stripe and WITHOUT changing any tier:
  1. a forged signature is rejected (400),
  2. a missing signature is rejected (400),
  3. a correctly self-signed event passes verification and reaches the handler (200).

Step 3 uses an UNKNOWN customer id, so the handler's key-back finds no project and changes
nothing (action 'skipped') — it never grants/revokes a real entitlement. The GRANT path is
NOT exercised here because it re-fetches the authoritative subscription from real Stripe; that
is covered offline by tests/test_billing_webhook_e2e.py via the injectable stub.

Requires DECOMPUTE_STRIPE_WEBHOOK_SECRET in the environment — the SAME secret the running
gateway loaded (so restart the gateway after setting it; uvicorn --reload does NOT re-source .env).

Usage:  set -a; source ./.env; set +a
        python scripts/billing_webhook_smoke.py [http://127.0.0.1:8080]
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
URL = BASE + "/v1/billing/stripe-webhook"
SECRET = os.environ.get("DECOMPUTE_STRIPE_WEBHOOK_SECRET")
if not SECRET:
    print("FAIL: DECOMPUTE_STRIPE_WEBHOOK_SECRET not set — `set -a; source ./.env; set +a` first.")
    sys.exit(2)


def _sign(payload: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(etype: str, obj: dict) -> bytes:
    return json.dumps({"id": f"evt_smoke_{int(time.time() * 1000)}", "object": "event",
                       "type": etype, "data": {"object": obj}}).encode()


def _post(payload: bytes, sig: str | None):
    headers = {"content-type": "application/json"}
    if sig:
        headers["stripe-signature"] = sig
    req = urllib.request.Request(URL, data=payload, method="POST", headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


failures: list[str] = []


def _check(label: str, got: int, want: int) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: HTTP {got} (want {want})")
    if not ok:
        failures.append(label)


def main() -> int:
    print(f"webhook signature smoke -> {URL}")
    forged = _event("checkout.session.completed", {"client_reference_id": "proj_default"})
    _check("forged signature rejected", _post(forged, "t=1,v1=deadbeef")[0], 400)
    _check("missing signature rejected", _post(forged, None)[0], 400)

    # Valid signature, UNKNOWN customer -> handler runs but changes nothing (action 'skipped').
    payload = _event("customer.subscription.deleted",
                     {"id": "sub_smoke_DELETEME", "customer": "cus_smoke_DELETEME"})
    code, body = _post(payload, _sign(payload))
    _check("valid signature accepted (reaches handler)", code, 200)
    print(f"        handler body: {body}")

    if failures:
        print(f"SMOKE FAILED: {failures}")
        return 1
    print("SMOKE PASSED: live webhook signature gate verified (no Stripe contacted, no tier changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
