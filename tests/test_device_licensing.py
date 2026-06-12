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
from decompute_gateway.licensing import LicenseService, device_fingerprint_from_request
from decompute_gateway import signed_license
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


# ── desktop-app license contract (headers + modal) ──────────────────────────

class _Hdr(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def test_device_fingerprint_multi_source():
    assert device_fingerprint_from_request(_Hdr({"x-decompute-device-fingerprint": "a"})) == "a"
    assert device_fingerprint_from_request(_Hdr({"x-decompute-device": "b"})) == "b"
    assert device_fingerprint_from_request(_Hdr({"x-decompute-machine-id": "c"})) == "c"
    assert device_fingerprint_from_request(_Hdr({}), {"decompute": {"device_fingerprint": "d"}}) == "d"
    assert device_fingerprint_from_request(_Hdr({})) is None


def test_contract_headers_and_modal(tmp_path):
    st = _store(_settings(tmp_path, free_compression_quota=2))
    lic = LicenseService(st.settings, st)
    # under quota -> compression allowed, no modal, upgrade url present
    m1 = lic.resolve("dev-c")
    h1 = LicenseService.decision_headers(m1)
    assert h1["x-decompute-license-plan"] == "free"
    assert h1["x-decompute-compression-allowed"] == "true"
    assert h1["x-decompute-quota-free-remaining"] == "1"
    assert h1["x-decompute-upgrade-url"].startswith("https://decompute.run/pro")
    assert LicenseService.ui_metadata(m1)["ui"]["show_modal"] is False
    # exhaust -> paused, modal with both actions
    lic.resolve("dev-c")
    m3 = lic.resolve("dev-c")
    h3 = LicenseService.decision_headers(m3)
    assert h3["x-decompute-compression-paused"] == "true"
    assert h3["x-decompute-license-reason"] == "free_quota_exhausted"
    assert h3["x-decompute-compression-paused-until"]  # non-empty reset time
    ui = LicenseService.ui_metadata(m3)["ui"]
    assert ui["show_modal"] is True
    assert {a["id"] for a in ui["actions"]} == {"continue_without_pro", "continue_with_pro"}


def test_admin_issue_then_activate(tmp_path):
    """No-Stripe admin grant: issue a key, activate it on a device, device becomes Pro."""
    st = _store(_settings(tmp_path))
    lic = LicenseService(st.settings, st)
    issued = st.issue_license(stripe_checkout_session_id=None, stripe_customer_id=None,
                              stripe_subscription_id=None, tier="pro", current_period_end=None)
    key = issued["license_key"]
    assert key and key.startswith("DCMP-")
    dh = st.hash_device_fingerprint("dev-admin")
    assert st.activate_device_binding(raw_key=key, device_hash=dh)["ok"]
    assert lic.resolve("dev-admin")["plan"] == "pro"
    assert lic.resolve("dev-admin")["compression_allowed"] is True


def test_pro_decision_has_no_modal(tmp_path):
    st = _store(_settings(tmp_path, free_compression_quota=1))
    lic = LicenseService(st.settings, st)
    dh = st.hash_device_fingerprint("dev-prom")
    import datetime as _d
    st.issue_license(stripe_checkout_session_id="cs_pm", stripe_customer_id="c", stripe_subscription_id="s",
                     current_period_end=_d.datetime.now(_d.timezone.utc) + _d.timedelta(days=30), device_hash=dh)
    m = lic.resolve("dev-prom")
    assert m["plan"] == "pro"
    assert LicenseService.decision_headers(m)["x-decompute-license-status"] == "active"
    assert LicenseService.ui_metadata(m)["ui"]["show_modal"] is False


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


# ── offline signed-license file (local-engine Pro gate) ──────────────────────
# These prove the tamper-resistant path: a signed file grants Pro through resolve(), while a
# forged/expired/foreign/keyless file degrades to the free tier (never fail-open to unlimited).

def _signed_settings(tmp_path, pub_pem, kid, **over):
    import json as _json
    return _settings(
        tmp_path, free_compression_quota=3, signed_license_enabled=True,
        license_file=str(tmp_path / "license.dcmp"),
        license_public_keys_json=_json.dumps({kid: pub_pem}), **over,
    )


def _issue(priv, kid, fp, *, days_valid=365, tier="pro", device_fp=None):
    now = dt.datetime.now(dt.timezone.utc)
    return signed_license.sign_license(
        private_pem=priv, kid=kid, license_id="lic_e2e",
        device_id=signed_license.canonical_device_id(device_fp or fp),
        issued_at=now - dt.timedelta(days=1),
        expires_at=None if days_valid is None else now + dt.timedelta(days=days_valid),
        tier=tier,
    )


def test_signed_license_grants_pro_through_resolver(tmp_path):
    priv, pub = signed_license.generate_keypair()
    kid = signed_license.key_id(pub)
    st = _store(_signed_settings(tmp_path, pub, kid))
    (tmp_path / "license.dcmp").write_text(_issue(priv, kid, "dev-signed-1"))
    d = LicenseService(st.settings, st).resolve("dev-signed-1", consume=False)
    assert d["plan"] == "pro" and d["reason"] == "pro_signed_license" and d["compress"] is True


def test_forged_signed_license_is_ignored(tmp_path):
    # Attacker signs Pro with their OWN key but ships it under our real kid -> bad signature -> free.
    priv, pub = signed_license.generate_keypair()
    kid = signed_license.key_id(pub)
    attacker_priv, _ = signed_license.generate_keypair()
    st = _store(_signed_settings(tmp_path, pub, kid))
    (tmp_path / "license.dcmp").write_text(_issue(attacker_priv, kid, "dev-x"))
    d = LicenseService(st.settings, st).resolve("dev-x", consume=False)
    assert d["plan"] == "free"  # forgery rejected, fell through to free tier (not unlimited)


def test_signed_license_for_other_machine_is_ignored(tmp_path):
    priv, pub = signed_license.generate_keypair()
    kid = signed_license.key_id(pub)
    st = _store(_signed_settings(tmp_path, pub, kid))
    # License bound to a DIFFERENT device than the one making the request.
    (tmp_path / "license.dcmp").write_text(_issue(priv, kid, "dev-me", device_fp="dev-someone-else"))
    d = LicenseService(st.settings, st).resolve("dev-me", consume=False)
    assert d["plan"] == "free"


def test_expired_signed_license_is_ignored(tmp_path):
    priv, pub = signed_license.generate_keypair()
    kid = signed_license.key_id(pub)
    st = _store(_signed_settings(tmp_path, pub, kid))
    (tmp_path / "license.dcmp").write_text(_issue(priv, kid, "dev-exp", days_valid=-2))  # expired 2d ago
    d = LicenseService(st.settings, st).resolve("dev-exp", consume=False)
    assert d["plan"] == "free"


def test_signed_license_inert_without_trusted_key(tmp_path):
    # Hosted deployments ship no verify key -> the file is ignored entirely (existing DB path stands).
    priv, pub = signed_license.generate_keypair()
    kid = signed_license.key_id(pub)
    st = _store(_settings(tmp_path, license_file=str(tmp_path / "license.dcmp")))  # no public key json
    (tmp_path / "license.dcmp").write_text(_issue(priv, kid, "dev-nokey"))
    d = LicenseService(st.settings, st).resolve("dev-nokey", consume=False)
    assert d["plan"] == "free"
