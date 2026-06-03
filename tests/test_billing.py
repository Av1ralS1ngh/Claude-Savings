from __future__ import annotations

"""Billing / licensing tests — the security-critical core.

Covers: webhook signature verification (forged events rejected), license issuance on
checkout, downgrade on cancel, status-driven tier sync, and tier-based quota enforcement.
Outbound Stripe calls (checkout/portal) need live keys and are not exercised here.
"""

import hashlib
import hmac
import json
import time

from decompute_gateway.billing import BillingError, BillingService
from decompute_gateway.config import Settings
from decompute_gateway.db import Store
from decompute_gateway.rate_limits import RateLimitManager
import pytest


def _settings(tmp_path, **over):
    s = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/billing.db")
    for k, v in {"stripe_secret_key": "sk_test_x", "stripe_webhook_secret": "whsec_test",
                 "stripe_price_id": "price_X", "stripe_price_tier": "pro", **over}.items():
        try:
            setattr(s, k, v)
        except Exception:
            object.__setattr__(s, k, v)
    return s


def _store(settings) -> Store:
    st = Store(settings)
    st.create_schema_for_local_dev()
    return st


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(etype: str, obj: dict) -> bytes:
    # Stripe's event envelope carries object="event"; the SDK checks it during construct_event.
    return json.dumps({"id": "evt_1", "object": "event", "type": etype, "data": {"object": obj}}).encode()


def _sub(*, status="active", price="price_X", sub_id="sub_1", customer="cus_1", period_end=1893456000):
    """A Stripe subscription object shaped like the authoritative re-fetch returns."""
    return {"id": sub_id, "object": "subscription", "status": status, "customer": customer,
            "items": {"data": [{"price": {"id": price, "product": "prod_X"}, "current_period_end": period_end}]}}


def _svc(settings, store, sub=None):
    """BillingService whose authoritative re-fetch returns `sub` (defaults to a valid pro sub)."""
    fetch = (lambda sid: sub if sub is not None else _sub(sub_id=sid))
    return BillingService(settings, store, subscription_fetcher=fetch)


# ── webhook signature ─────────────────────────────────────────────────────────

def test_webhook_rejects_forged_signature(tmp_path) -> None:
    s = _settings(tmp_path)
    svc = BillingService(s, _store(s))
    payload = _event("checkout.session.completed", {"client_reference_id": "proj_default"})
    with pytest.raises(BillingError):
        svc.construct_event(payload, "t=123,v1=deadbeef")  # wrong signature
    with pytest.raises(BillingError):
        svc.construct_event(payload, None)                 # missing signature


def test_webhook_accepts_valid_signature(tmp_path) -> None:
    s = _settings(tmp_path)
    svc = BillingService(s, _store(s))
    payload = _event("checkout.session.completed", {"client_reference_id": "proj_default"})
    event = svc.construct_event(payload, _sign(payload, "whsec_test"))
    assert event["type"] == "checkout.session.completed"


# ── license issuance / revocation (handle_event, with authoritative re-fetch) ──

def test_checkout_completed_grants_tier(tmp_path) -> None:
    s = _settings(tmp_path)
    st = _store(s)
    svc = _svc(s, st, sub=_sub(status="active", price="price_X", customer="cus_1"))
    assert st.get_project_tier("proj_default") == "free"
    res = svc.handle_event({"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": "proj_default", "customer": "cus_1", "subscription": "sub_1"}}})
    assert res["action"] == "granted" and res["tier"] == "pro"
    b = st.get_project_billing("proj_default")
    assert b["tier"] == "pro" and b["stripe_customer_id"] == "cus_1" and b["stripe_subscription_id"] == "sub_1"


def test_checkout_wrong_price_does_NOT_grant(tmp_path) -> None:
    """CRITICAL: a subscription for a different price must not grant the paid tier."""
    s = _settings(tmp_path)
    st = _store(s)
    svc = _svc(s, st, sub=_sub(status="active", price="price_SOMETHING_ELSE"))
    res = svc.handle_event({"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": "proj_default", "customer": "cus_1", "subscription": "sub_1"}}})
    assert res["action"] == "linked" and st.get_project_tier("proj_default") == "free"


def test_unpaid_checkout_does_NOT_grant(tmp_path) -> None:
    """CRITICAL: an incomplete/unpaid subscription (async payment) must not grant access."""
    s = _settings(tmp_path)
    st = _store(s)
    svc = _svc(s, st, sub=_sub(status="incomplete", price="price_X"))
    svc.handle_event({"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": "proj_default", "customer": "cus_1", "subscription": "sub_1"}}})
    assert st.get_project_tier("proj_default") == "free"


def test_stale_active_after_cancel_stays_free(tmp_path) -> None:
    """CRITICAL (ordering/replay): a redelivered 'active' event after cancel must NOT re-grant —
    the handler re-fetches the authoritative subscription, which is canceled."""
    s = _settings(tmp_path)
    st = _store(s)
    st.apply_subscription(project_id="proj_default", tier="free", status="canceled", stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    # Stripe re-delivers a stale updated(status=active) event, but the live sub is canceled.
    svc = _svc(s, st, sub=_sub(status="canceled", customer="cus_1"))
    svc.handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active"}}})  # stale payload says active
    assert st.get_project_tier("proj_default") == "free"


def test_subscription_deleted_revokes_to_free(tmp_path) -> None:
    s = _settings(tmp_path)
    st = _store(s)
    svc = _svc(s, st)
    st.apply_subscription(project_id="proj_default", tier="pro", status="active", stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    res = svc.handle_event({"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    assert res["action"] == "revoked" and st.get_project_tier("proj_default") == "free"


def test_subscription_status_drives_tier(tmp_path) -> None:
    s = _settings(tmp_path)
    st = _store(s)
    st.set_stripe_customer(project_id="proj_default", customer_id="cus_1")
    # past_due keeps the paid tier (dunning grace) when the live sub is past_due with our price...
    _svc(s, st, sub=_sub(status="past_due")).handle_event({"type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    assert st.get_project_tier("proj_default") == "pro"
    # ...and a canceled live sub drops to free.
    _svc(s, st, sub=_sub(status="canceled")).handle_event({"type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1", "customer": "cus_1"}}})
    assert st.get_project_tier("proj_default") == "free"


def test_webhook_keys_back_by_customer_when_no_reference(tmp_path) -> None:
    """A subscription event with only a customer id must still find the project."""
    s = _settings(tmp_path)
    st = _store(s)
    st.set_stripe_customer(project_id="proj_default", customer_id="cus_42")
    svc = _svc(s, st, sub=_sub(status="active", customer="cus_42"))
    res = svc.handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_9", "customer": "cus_42"}}})
    assert res["project_id"] == "proj_default" and st.get_project_tier("proj_default") == "pro"


def test_apply_subscription_refuses_customer_rebind(tmp_path) -> None:
    """A project bound to one customer must not be silently rebound to another."""
    s = _settings(tmp_path)
    st = _store(s)
    st.apply_subscription(project_id="proj_default", tier="pro", status="active", stripe_customer_id="cus_1")
    st.apply_subscription(project_id="proj_default", tier="pro", status="active", stripe_customer_id="cus_HIJACK")
    assert st.get_project_billing("proj_default")["stripe_customer_id"] == "cus_1"


def test_lapsed_period_self_heals_to_free(tmp_path) -> None:
    """Backstop: if the paid period lapsed beyond grace (missed deletion webhook), read as free."""
    import datetime as dt
    s = _settings(tmp_path, billing_grace_days=3)
    st = _store(s)
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    st.apply_subscription(project_id="proj_default", tier="pro", status="active", current_period_end=long_ago)
    assert st.get_project_tier("proj_default") == "free"


def test_unknown_event_is_noop(tmp_path) -> None:
    s = _settings(tmp_path)
    svc = _svc(s, _store(s))
    assert svc.handle_event({"type": "invoice.paid", "data": {"object": {}}})["handled"] is False


# ── enforcement: tier drives quota ──────────────────────────────────────────────

def test_tier_drives_effective_limits(tmp_path) -> None:
    s = _settings(tmp_path)
    st = _store(s)
    rl = RateLimitManager(s, store=st)
    free = rl.effective_limits("proj_default")
    assert free["per_key_rpm"] == RateLimitManager.TIER_LIMITS["free"]["per_key_rpm"]
    st.apply_subscription(project_id="proj_default", tier="pro", status="active")
    pro = rl.effective_limits("proj_default")
    assert pro["per_key_rpm"] == RateLimitManager.TIER_LIMITS["pro"]["per_key_rpm"]
    assert pro["project_tokens_per_min"] > free["project_tokens_per_min"]


def test_no_store_keeps_flat_settings(tmp_path) -> None:
    """Back-compat: without a store, limits come from flat settings (existing behaviour)."""
    s = _settings(tmp_path)
    rl = RateLimitManager(s)  # no store
    assert rl.effective_limits("proj_default")["per_key_rpm"] == s.per_key_rpm
