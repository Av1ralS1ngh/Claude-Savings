from __future__ import annotations

"""End-to-end proof of the Stripe purchase -> entitlement loop through the REAL HTTP
routes (/v1/billing/stripe-webhook + /v1/billing/status), not just the service layer.

This closes the P0 gap: the grant/revoke LOGIC was unit-tested (test_billing.py), but the
live wire — signature verification on the route, the route -> handler -> DB tier write, and
the status read reflecting it — was unproven. No real Stripe is contacted: the authoritative
subscription re-fetch is the injectable stub (_svc), and signatures are self-signed with the
test webhook secret. Mirrors how Stripe would actually deliver events.
"""

import json
import os
import tempfile

# Make importing decompute_gateway.main safe and isolated BEFORE it loads: a throwaway sqlite
# (never the real Postgres) and local/no-auth posture. load_dotenv(override=False) means these
# preset vars win over .env.
os.environ.setdefault("DECOMPUTE_DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mktemp(suffix='.db')}")
os.environ.setdefault("DECOMPUTE_ENV", "local")
os.environ.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "true")
os.environ.setdefault("DECOMPUTE_REQUIRE_AUTH", "false")

import pytest
from fastapi.testclient import TestClient

import decompute_gateway.main as M
from decompute_gateway.rate_limits import RateLimitManager

# Reuse the signing/store helpers from the sibling module. Bare import (pytest's default
# prepend importmode puts tests/ on sys.path) — NOT `from tests.test_billing import ...`,
# because tests/ is not a package and is not on the configured pythonpath.
from test_billing import _settings, _store, _sign, _sub, _svc


def _event_id(etype: str, obj: dict, eid: str) -> bytes:
    """Stripe event envelope with an EXPLICIT id. The shared _event helper hardcodes evt_1,
    but the handler dedups on event id (mark_stripe_event), so grant/replay must share an id
    and revoke must use a different one or it would be silently swallowed as a replay."""
    return json.dumps({"id": eid, "object": "event", "type": etype, "data": {"object": obj}}).encode()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """A TestClient over the real app, with module globals pointed at a fresh sqlite store and
    a BillingService whose authoritative re-fetch returns an ACTIVE pro subscription."""
    settings = _settings(tmp_path)  # whsec_test / sk_test_x / price_X / tier=pro, fresh sqlite
    store = _store(settings)        # schema + proj_default (tier=free)
    billing = _svc(settings, store, sub=_sub(status="active", price="price_X",
                                             customer="cus_1", sub_id="sub_1"))
    monkeypatch.setattr(M, "settings", settings)
    monkeypatch.setattr(M, "store", store)
    monkeypatch.setattr(M, "billing", billing)
    monkeypatch.setattr(M, "rate_limits", RateLimitManager(settings, store=store))
    return TestClient(M.app), store


def _post_webhook(client, payload: bytes, sig: str | None, route: str = "/v1/billing/stripe-webhook"):
    headers = {"content-type": "application/json"}
    if sig is not None:
        headers["stripe-signature"] = sig
    return client.post(route, content=payload, headers=headers)


def test_webhook_rejects_unsigned_and_forged_on_both_routes(wired):
    client, _ = wired
    payload = _event_id("checkout.session.completed", {"client_reference_id": "proj_default"}, "evt_x")
    # Forged signature -> 400.
    assert _post_webhook(client, payload, "t=1,v1=deadbeef").status_code == 400
    # Missing signature -> 400.
    assert _post_webhook(client, payload, None).status_code == 400
    # The boss-spec alias shares the same handler -> also rejects a forged signature.
    assert _post_webhook(client, payload, "t=1,v1=deadbeef", route="/v1/billing/webhook").status_code == 400


def test_full_purchase_to_entitlement_loop(wired):
    client, store = wired
    assert store.get_project_tier("proj_default") == "free"

    # B: GRANT — a signed checkout.session.completed flips the project to Pro.
    grant = _event_id(
        "checkout.session.completed",
        {"client_reference_id": "proj_default", "customer": "cus_1", "subscription": "sub_1"},
        "evt_co_1",
    )
    r = _post_webhook(client, grant, _sign(grant, "whsec_test"))
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "granted"
    status = client.get("/v1/billing/status").json()
    assert status["tier"] == "pro" and status["stripe_subscription_id"] == "sub_1"

    # Replay the SAME event id — must be idempotent (deduped, no re-grant). The route doesn't
    # surface 'deduped', so prove it via action==null and the tier staying pro.
    r2 = _post_webhook(client, grant, _sign(grant, "whsec_test"))
    assert r2.status_code == 200
    assert r2.json().get("action") is None
    assert client.get("/v1/billing/status").json()["tier"] == "pro"

    # C: REVOKE — a signed customer.subscription.deleted (DISTINCT event id) downgrades to free.
    # It keys back to the project via the customer id bound during GRANT, so order matters.
    revoke = _event_id("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}, "evt_del_1")
    r3 = _post_webhook(client, revoke, _sign(revoke, "whsec_test"))
    assert r3.status_code == 200, r3.text
    assert r3.json()["action"] == "revoked"
    assert client.get("/v1/billing/status").json()["tier"] == "free"
