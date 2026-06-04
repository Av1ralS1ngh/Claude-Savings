from __future__ import annotations

"""Device-licensing tests (the BYOK/self-hosted monetization axis).

Covers the security-critical invariants: server-derived device hashing, atomic free-quota
consumption + graceful degrade, single-redeem (one payment -> one license), device binding /
rebind, webhook event dedup, and redeem rejecting unpaid/wrong-SKU sessions. Plus an end-to-end
request-path check that a free device degrades to no-compression once its quota is spent while a
fresh device still compresses, and that hosted (api_key) callers are never device-gated.
"""

import datetime as dt

from decompute_gateway.billing import BillingError, BillingService
from decompute_gateway.config import Settings
from decompute_gateway.db import Store
from decompute_gateway.licensing import LicenseService
import pytest


def _settings(tmp_path, **over):
    s = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/lic.db")
    base = {"stripe_secret_key": "sk_test_x", "stripe_webhook_secret": "whsec_test",
            "stripe_price_id": "price_X", "stripe_price_tier": "pro",
            "device_fingerprint_salt": "test-device-salt", "license_key_hash_salt": "test-lic-salt",
            "free_compression_quota": 3, **over}
    for k, v in base.items():
        try:
            setattr(s, k, v)
        except Exception:
            object.__setattr__(s, k, v)
    return s


def _store(settings) -> Store:
    st = Store(settings)
    st.create_schema_for_local_dev()
    return st


def _sub(*, status="active", price="price_X", sub_id="sub_1", customer="cus_1", period_end=1893456000, device_hash=None):
    meta = {"device_hash": device_hash} if device_hash else {}
    return {"id": sub_id, "object": "subscription", "status": status, "customer": customer, "metadata": meta,
            "items": {"data": [{"price": {"id": price, "product": "prod_X"}, "current_period_end": period_end}]}}


def _checkout(*, paid=True, sub=None, customer="cus_1"):
    return {"id": "cs_x", "object": "checkout.session", "payment_status": "paid" if paid else "unpaid",
            "customer": customer, "subscription": sub or _sub()}


def _svc(settings, store, *, sub=None, session=None):
    fetch_sub = (lambda sid: sub if sub is not None else _sub(sub_id=sid))
    fetch_sess = (lambda sid: session if session is not None else _checkout())
    return BillingService(settings, store, subscription_fetcher=fetch_sub, checkout_session_fetcher=fetch_sess)


# ── hashing + quota ──────────────────────────────────────────────────────────

def test_device_hash_deterministic_and_salted(tmp_path):
    st = _store(_settings(tmp_path))
    st2 = _store(_settings(tmp_path, device_fingerprint_salt="OTHER"))
    assert st.hash_device_fingerprint("dev-1") == st.hash_device_fingerprint("dev-1")
    assert len(st.hash_device_fingerprint("dev-1")) == 64
    # A different pepper yields a different hash (so the digest can't be precomputed).
    assert st.hash_device_fingerprint("dev-1") != st2.hash_device_fingerprint("dev-1")


def test_resolve_is_fail_open(tmp_path):
    """If the licensing store errors (e.g. migration not yet applied), the request must NOT
    break — resolve returns None (ungated) so the proxy proceeds with compression on."""
    class _BoomStore:
        def hash_device_fingerprint(self, fp):
            return "h" * 64
        def get_active_license_for_device(self, dh):
            raise RuntimeError("relation \"licenses\" does not exist")
    lic = LicenseService(_settings(tmp_path), _BoomStore())
    assert lic.resolve("dev-x") is None  # fail-open, no exception


def test_free_quota_consumes_then_degrades(tmp_path):
    st = _store(_settings(tmp_path, free_compression_quota=3))
    lic = LicenseService(st.settings, st)
    decisions = [lic.resolve("dev-a")["compress"] for _ in range(5)]
    assert decisions == [True, True, True, False, False]  # 3 allowed, then degrade to off


def test_free_quota_atomic_no_overspend(tmp_path):
    """The conditional increment must never admit more than `limit` consumes."""
    st = _store(_settings(tmp_path, free_compression_quota=5))
    dh = st.hash_device_fingerprint("dev-race")
    granted = sum(1 for _ in range(20) if st.try_consume_free_quota(dh)["consumed"])
    assert granted == 5


def test_free_quota_resets_next_window(tmp_path):
    st = _store(_settings(tmp_path, free_compression_quota=2))
    dh = st.hash_device_fingerprint("dev-win")
    today = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    tomorrow = today + dt.timedelta(days=1)
    assert st.try_consume_free_quota(dh, now=today)["consumed"]
    assert st.try_consume_free_quota(dh, now=today)["consumed"]
    assert not st.try_consume_free_quota(dh, now=today)["consumed"]  # spent today
    assert st.try_consume_free_quota(dh, now=tomorrow)["consumed"]   # fresh window


# ── license issuance / binding ───────────────────────────────────────────────

def test_issue_license_single_redeem(tmp_path):
    """One paid checkout session mints exactly one license; replay returns the same one."""
    st = _store(_settings(tmp_path))
    dh = st.hash_device_fingerprint("dev-p")
    r1 = st.issue_license(stripe_checkout_session_id="cs_1", stripe_customer_id="cus_1",
                          stripe_subscription_id="sub_1", device_hash=dh)
    assert r1["license_key"] and not r1["already_issued"]
    r2 = st.issue_license(stripe_checkout_session_id="cs_1", stripe_customer_id="cus_1",
                          stripe_subscription_id="sub_1", device_hash=dh)
    assert r2["already_issued"] and r2["license_key"] is None  # never a second key


def test_pro_device_always_compresses(tmp_path):
    st = _store(_settings(tmp_path, free_compression_quota=1))
    lic = LicenseService(st.settings, st)
    dh = st.hash_device_fingerprint("dev-pro")
    st.issue_license(stripe_checkout_session_id="cs_2", stripe_customer_id="cus_1", stripe_subscription_id="sub_2",
                     current_period_end=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30), device_hash=dh)
    assert all(lic.resolve("dev-pro")["compress"] for _ in range(10))
    assert lic.resolve("dev-pro")["tier"] == "pro"


def test_lapsed_license_self_heals_to_free(tmp_path):
    st = _store(_settings(tmp_path, billing_grace_days=3))
    dh = st.hash_device_fingerprint("dev-lapsed")
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    st.issue_license(stripe_checkout_session_id="cs_3", stripe_customer_id="cus_1", stripe_subscription_id="sub_3",
                     current_period_end=long_ago, device_hash=dh)
    assert st.get_active_license_for_device(dh) is None  # period lapsed beyond grace


def test_activate_binds_and_rejects_bad_key(tmp_path):
    st = _store(_settings(tmp_path))
    r = st.issue_license(stripe_checkout_session_id="cs_4", stripe_customer_id="cus_1", stripe_subscription_id="sub_4",
                         current_period_end=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30))
    key = r["license_key"]
    dh = st.hash_device_fingerprint("dev-act")
    assert st.activate_device_binding(raw_key=key, device_hash=dh)["ok"]
    assert st.get_active_license_for_device(dh) is not None
    # migrate to a new device — old device loses Pro (single-device binding)
    dh2 = st.hash_device_fingerprint("dev-act-2")
    assert st.activate_device_binding(raw_key=key, device_hash=dh2)["ok"]
    assert st.get_active_license_for_device(dh) is None
    assert st.get_active_license_for_device(dh2) is not None
    # a forged/unknown key is rejected
    assert st.activate_device_binding(raw_key="DCMP-forged", device_hash=dh)["reason"] == "invalid_key"


def test_revoke_drops_device_to_free(tmp_path):
    st = _store(_settings(tmp_path))
    dh = st.hash_device_fingerprint("dev-rev")
    st.issue_license(stripe_checkout_session_id="cs_5", stripe_customer_id="cus_1", stripe_subscription_id="sub_5",
                     current_period_end=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30), device_hash=dh)
    assert st.revoke_licenses_for_subscription("sub_5") == 1
    assert st.get_active_license_for_device(dh) is None


# ── Stripe paths (redeem + webhook) ──────────────────────────────────────────

def test_redeem_rejects_unpaid_and_wrong_sku(tmp_path):
    s = _settings(tmp_path); st = _store(s)
    dh = st.hash_device_fingerprint("dev-rd")
    with pytest.raises(BillingError):
        _svc(s, st, session=_checkout(paid=False)).redeem_checkout(session_id="cs_u", device_hash=dh)
    with pytest.raises(BillingError):
        _svc(s, st, session=_checkout(paid=True, sub=_sub(price="price_OTHER"))).redeem_checkout(session_id="cs_w", device_hash=dh)
    assert st.get_active_license_for_device(dh) is None  # nothing granted


def test_redeem_issues_once_and_binds(tmp_path):
    s = _settings(tmp_path); st = _store(s)
    dh = st.hash_device_fingerprint("dev-rd2")
    svc = _svc(s, st, session=_checkout(paid=True, sub=_sub(price="price_X")))
    r = svc.redeem_checkout(session_id="cs_ok", device_hash=dh)
    assert r["license_key"] and r["device_activated"] and r["tier"] == "pro"
    assert st.get_active_license_for_device(dh) is not None
    r2 = svc.redeem_checkout(session_id="cs_ok", device_hash=dh)
    assert r2["already_issued"] and r2["license_key"] is None


def test_webhook_issues_device_license_and_dedups(tmp_path):
    s = _settings(tmp_path); st = _store(s)
    dh = st.hash_device_fingerprint("dev-wh")
    svc = _svc(s, st, sub=_sub(status="active", price="price_X", device_hash=dh))
    evt = {"id": "evt_dl_1", "object": "event", "type": "checkout.session.completed",
           "data": {"object": {"id": "cs_wh", "customer": "cus_1", "subscription": "sub_1", "metadata": {"device_hash": dh}}}}
    res = svc.handle_event(evt)
    assert res["device"] and res["action"] == "granted"
    assert st.get_active_license_for_device(dh) is not None
    # redelivery is deduped (same event id) -> no double issue
    assert svc.handle_event(evt).get("deduped") is True


def test_webhook_deleted_revokes_device_license(tmp_path):
    s = _settings(tmp_path); st = _store(s)
    dh = st.hash_device_fingerprint("dev-whd")
    st.issue_license(stripe_checkout_session_id="cs_whd", stripe_customer_id="cus_1", stripe_subscription_id="sub_9",
                     current_period_end=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30), device_hash=dh)
    svc = _svc(s, st)
    evt = {"id": "evt_del_1", "object": "event", "type": "customer.subscription.deleted",
           "data": {"object": {"id": "sub_9", "metadata": {"device_hash": dh}}}}
    res = svc.handle_event(evt)
    assert res.get("device") and res["action"] == "revoked"
    assert st.get_active_license_for_device(dh) is None


def test_device_purchase_does_not_change_project_tier(tmp_path):
    """A device-license checkout must never bump proj_default's tier (axes stay orthogonal)."""
    s = _settings(tmp_path); st = _store(s)
    dh = st.hash_device_fingerprint("dev-iso")
    svc = _svc(s, st, sub=_sub(status="active", price="price_X", device_hash=dh))
    svc.handle_event({"id": "evt_iso", "object": "event", "type": "checkout.session.completed",
                      "data": {"object": {"id": "cs_iso", "customer": "cus_1", "subscription": "sub_1",
                                          "metadata": {"project_id": "proj_default", "device_hash": dh}}}})
    assert st.get_project_tier("proj_default") == "free"  # project tier untouched


# ── end-to-end request path (the load-bearing hook) ──────────────────────────

def _big_tool_body():
    log = "\n".join(f"2026-06-04 INFO step {i} compiling pkg_{i % 30} ok bytes={4096 + i}" for i in range(800))
    return {"model": "claude-haiku-4-5", "max_tokens": 8, "messages": [
        {"role": "user", "content": "build"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": log}]},
        {"role": "user", "content": "summarize"}]}


def test_request_path_free_compresses_then_degrades():
    import os
    os.environ.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "true")
    os.environ.setdefault("DECOMPUTE_ENV", "local")
    os.environ.setdefault("DECOMPUTE_DATABASE_URL", "sqlite+pysqlite:///.decompute/decompute.db")
    from fastapi.testclient import TestClient
    from decompute_gateway.main import app, settings, store

    if not settings.mock_anthropic:
        pytest.skip("request-path check runs only in mock mode")
    client = TestClient(app)
    dev = "dev-itest-degrade"

    # Fresh device, under quota -> compression ON, license headers present.
    r1 = client.post("/v1/messages", json=_big_tool_body(), headers={"x-decompute-device": dev})
    assert r1.status_code == 200, r1.text
    assert r1.headers.get("x-decompute-tier") == "free"
    assert r1.headers.get("x-decompute-quota-remaining") is not None
    assert int(r1.headers["x-decompute-tokens-saved"]) > 0  # the big tool_result was compressed

    # Spend the rest of this device's daily quota -> next request degrades to no-compression.
    dh = store.hash_device_fingerprint(dev)
    for _ in range(int(settings.free_compression_quota) + 1):
        store.try_consume_free_quota(dh)
    r2 = client.post("/v1/messages", json=_big_tool_body(), headers={"x-decompute-device": dev})
    assert r2.status_code == 200
    assert int(r2.headers["x-decompute-tokens-saved"]) == 0  # graceful degrade, still 200

    # No device header -> ungated (local/dev keeps working), and no license headers.
    r3 = client.post("/v1/messages", json=_big_tool_body())
    assert int(r3.headers["x-decompute-tokens-saved"]) > 0
    assert "x-decompute-tier" not in r3.headers


def test_metered_api_key_caller_is_not_device_gated():
    import os
    os.environ.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "true")
    os.environ.setdefault("DECOMPUTE_ENV", "local")
    os.environ.setdefault("DECOMPUTE_DATABASE_URL", "sqlite+pysqlite:///.decompute/decompute.db")
    from fastapi.testclient import TestClient
    from decompute_gateway.main import app, settings, store

    if not settings.mock_anthropic:
        pytest.skip("request-path check runs only in mock mode")
    client = TestClient(app)
    key = store.create_api_key(project_id="proj_default", name="lic-test", scopes=["messages:write", "usage:read", "admin"])["api_key"]
    dev = "dev-itest-metered"
    # Even with a device header, a hosted (api_key) caller is on the project-tier path -> no
    # device gating, no license headers.
    r = client.post("/v1/messages", json=_big_tool_body(), headers={"x-decompute-api-key": key, "x-decompute-device": dev})
    assert r.status_code == 200
    assert "x-decompute-tier" not in r.headers
