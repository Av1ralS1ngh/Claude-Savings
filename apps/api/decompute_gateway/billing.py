from __future__ import annotations

"""Stripe billing + license issuance for the Decompute gateway.

Model: a paid Stripe subscription IS the license. Buying the configured price upgrades a
project's `tier` (free -> pro/enterprise), which drives its quota (see rate_limits.TIER_LIMITS).
The `dc_` API key remains the credential; the subscription is the entitlement.

Flow:
  decompute upgrade  -> POST /v1/billing/checkout -> Stripe Checkout (client_reference_id=project_id)
  user pays          -> Stripe -> POST /v1/billing/webhook (signature-verified)
                          checkout.session.completed      -> link customer/sub + grant tier  (ISSUE)
                          customer.subscription.updated   -> sync status/tier/period
                          customer.subscription.deleted   -> downgrade to free               (REVOKE)
  decompute billing  -> GET /v1/billing/status / POST /v1/billing/portal (manage)

This module never trusts client input for entitlements: tier changes happen ONLY from a
signature-verified Stripe webhook, keyed back to the project via client_reference_id /
the stored stripe_customer_id.
"""

import datetime as _dt
from typing import Any

import stripe

# Subscription statuses that keep the paid tier live. Anything else falls back to free.
ACTIVE_STATUSES = {"active", "trialing", "past_due"}  # past_due keeps access during Stripe's dunning grace


class BillingError(Exception):
    """Raised for billing misconfiguration or Stripe API failures (mapped to HTTP by the route)."""


class BillingService:
    def __init__(self, settings: Any, store: Any, subscription_fetcher=None, checkout_session_fetcher=None):
        self.settings = settings
        self.store = store
        # Re-fetches the AUTHORITATIVE subscription/session from Stripe. Injectable for tests.
        self._fetch_subscription = subscription_fetcher or self._stripe_fetch_subscription
        self._fetch_checkout_session = checkout_session_fetcher or self._stripe_fetch_checkout_session

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "billing_enabled", False))

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise BillingError("billing is not configured (no Stripe secret key)")
        stripe.api_key = self.settings.stripe_secret_key
        if getattr(self.settings, "stripe_api_version", None):
            stripe.api_version = self.settings.stripe_api_version

    # ── entitlement derivation (the security-critical part) ───────────────────
    def _stripe_fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        self._require_enabled()
        sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.price"])
        return sub if isinstance(sub, dict) else sub.to_dict()

    def _stripe_fetch_checkout_session(self, session_id: str) -> dict[str, Any]:
        self._require_enabled()
        sess = stripe.checkout.Session.retrieve(session_id, expand=["subscription", "subscription.items.data.price"])
        return sess if isinstance(sess, dict) else sess.to_dict()

    def _sub_has_configured_price(self, sub: dict[str, Any]) -> bool:
        """True only if the subscription actually carries the price/product we sell.

        Without this, ANY active subscription on the account (a cheaper SKU, an add-on, a
        100%-off coupon, a free trial) would otherwise grant the paid tier.
        """
        want_price = self.settings.stripe_price_id
        want_product = self.settings.stripe_product_id
        for item in ((sub.get("items") or {}).get("data") or []):
            price = item.get("price") or {}
            if want_price and price.get("id") == want_price:
                return True
            if want_product and price.get("product") == want_product:
                return True
        return False

    @staticmethod
    def _period_end(sub: dict[str, Any]) -> _dt.datetime | None:
        # current_period_end moved from the Subscription to its items in Stripe 2025+; try both.
        cpe = sub.get("current_period_end")
        if not isinstance(cpe, (int, float)):
            items = (sub.get("items") or {}).get("data") or []
            cpe = items[0].get("current_period_end") if items else None
        return _dt.datetime.fromtimestamp(cpe, tz=_dt.timezone.utc) if isinstance(cpe, (int, float)) else None

    def _tier_for(self, sub: dict[str, Any]) -> tuple[str, str | None, _dt.datetime | None]:
        """Authoritative (tier, status, period_end) for a subscription object.

        Paid tier requires BOTH an active-ish status AND the configured price — otherwise free.
        """
        status = sub.get("status")
        period_end = self._period_end(sub)
        if status in ACTIVE_STATUSES and self._sub_has_configured_price(sub):
            return self.settings.stripe_price_tier, status, period_end
        return "free", status, period_end

    def _apply_subscription_obj(self, project_id: str, sub: dict[str, Any], customer: str | None) -> dict[str, Any]:
        tier, status, period_end = self._tier_for(sub)
        self.store.apply_subscription(
            project_id=project_id, tier=tier, status=status,
            stripe_customer_id=customer, stripe_subscription_id=sub.get("id"),
            current_period_end=period_end,
        )
        return {"tier": tier, "status": status}

    # ── outbound Stripe calls ────────────────────────────────────────────────
    def create_checkout_session(self, *, project_id: str, customer_email: str | None = None,
                                device_hash: str | None = None) -> dict[str, Any]:
        """Create a subscription Checkout Session; returns {url, id}.

        Two flavors share this: a hosted PROJECT-tier purchase (client_reference_id=project_id),
        and a self-hosted DEVICE-license purchase (device_hash in metadata) so the webhook/redeem
        can bind the issued license to the device. Both can be set; the handlers branch on which.
        """
        self._require_enabled()
        if not self.settings.stripe_price_id:
            raise BillingError("no Stripe price configured")
        billing = self.store.get_project_billing(project_id) or {}
        meta = {"project_id": project_id}
        if device_hash:
            meta["device_hash"] = device_hash
        kwargs: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": self.settings.stripe_price_id, "quantity": 1}],
            "success_url": self.settings.billing_success_url,
            "cancel_url": self.settings.billing_cancel_url,
            # The link back — the webhook reads this to know who paid.
            "client_reference_id": project_id,
            "metadata": dict(meta),
            "subscription_data": {"metadata": dict(meta)},
        }
        if billing.get("stripe_customer_id"):
            kwargs["customer"] = billing["stripe_customer_id"]
        elif customer_email:
            kwargs["customer_email"] = customer_email
        try:
            session = stripe.checkout.Session.create(**kwargs)
        except Exception as exc:  # stripe.error.StripeError and friends
            raise BillingError(f"Stripe checkout failed: {exc}")
        return {"url": session.url, "id": session.id}

    # ── device-license redemption ─────────────────────────────────────────────
    def redeem_checkout(self, *, session_id: str, device_hash: str | None = None) -> dict[str, Any]:
        """Verify a PAID Checkout Session server-side and issue a device Pro license exactly once.

        Re-fetches the authoritative Session + Subscription from Stripe (never trusts the client's
        session id beyond addressing), requires payment_status=='paid' AND the configured price,
        then mints a license (idempotent on session_id) and, if a device fingerprint is present,
        binds it. Returns {license_key (once), tier, device_activated, already_issued}."""
        self._require_enabled()
        try:
            session = self._fetch_checkout_session(session_id)
        except Exception as exc:
            raise BillingError(f"could not retrieve checkout session: {exc}")
        if session.get("payment_status") != "paid":
            raise BillingError("checkout session is not paid")
        sub = session.get("subscription")
        if isinstance(sub, str):
            sub = self._fetch_subscription(sub)
        if not isinstance(sub, dict):
            raise BillingError("checkout session has no subscription")
        tier, status, period_end = self._tier_for(sub)
        if tier == "free":
            # Wrong SKU / inactive — never issue a Pro license for an unconfigured price.
            raise BillingError("subscription does not carry the configured Pro price")
        customer = session.get("customer") if isinstance(session.get("customer"), str) else sub.get("customer")
        res = self.store.issue_license(
            stripe_checkout_session_id=session_id,
            stripe_customer_id=customer if isinstance(customer, str) else None,
            stripe_subscription_id=sub.get("id"), tier=tier,
            current_period_end=period_end, device_hash=device_hash,
        )
        return {
            "license_key": res.get("license_key"), "tier": res.get("tier", tier),
            "already_issued": res.get("already_issued", False),
            "device_activated": bool(device_hash),  # issue binds on create and rebinds on replay
            "current_period_end": period_end.isoformat() if period_end else None,
        }

    def create_portal_session(self, *, project_id: str) -> dict[str, Any]:
        """Create a Billing Portal session so the user can manage/cancel; returns {url}."""
        self._require_enabled()
        billing = self.store.get_project_billing(project_id) or {}
        customer = billing.get("stripe_customer_id")
        if not customer:
            raise BillingError("no Stripe customer for this project — subscribe first")
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer, return_url=self.settings.billing_portal_return_url
            )
        except Exception as exc:
            raise BillingError(f"Stripe portal failed: {exc}")
        return {"url": session.url}

    # ── inbound webhook ──────────────────────────────────────────────────────
    def construct_event(self, payload: bytes, sig_header: str | None) -> dict[str, Any]:
        """Verify + parse a webhook payload. Raises BillingError on a bad/forged signature."""
        secret = self.settings.stripe_webhook_secret
        if not secret:
            raise BillingError("no Stripe webhook secret configured")
        try:
            event = stripe.Webhook.construct_event(payload, sig_header or "", secret)
        except Exception as exc:  # ValueError (bad payload) / SignatureVerificationError
            raise BillingError(f"invalid webhook signature: {exc}")
        return event if isinstance(event, dict) else event.to_dict()

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Apply a verified Stripe event to the project's license. Idempotent.

        Returns a small dict describing what changed (for logging/audit). Tier is granted or
        revoked here and ONLY here, never from a client request.
        """
        etype = event.get("type", "")
        # Idempotency: a redelivered event (same id) must not double-issue a license.
        if not self.store.mark_stripe_event(event.get("id"), etype):
            return {"handled": True, "type": etype, "deduped": True}
        obj = (event.get("data") or {}).get("object") or {}
        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "customer.subscription.created": self._on_subscription_change,
            "customer.subscription.updated": self._on_subscription_change,
            "customer.subscription.deleted": self._on_subscription_deleted,
        }.get(etype)
        if not handler:
            return {"handled": False, "type": etype}
        return handler(obj) | {"handled": True, "type": etype}

    @staticmethod
    def _device_hash_from(obj: dict[str, Any]) -> str | None:
        return (obj.get("metadata") or {}).get("device_hash")

    def _project_id_from(self, obj: dict[str, Any]) -> str | None:
        # Prefer the explicit reference set at checkout; fall back to metadata, then customer lookup.
        pid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("project_id")
        if pid:
            return pid
        customer = obj.get("customer")
        if isinstance(customer, str) and customer:
            return self.store.find_project_by_customer(customer)
        return None

    def _on_checkout_completed(self, session: dict[str, Any]) -> dict[str, Any]:
        # A device-license purchase (device_hash in metadata) issues a DEVICE license and never
        # touches the project tier. Otherwise it's a hosted PROJECT-tier purchase (existing path).
        device_hash = self._device_hash_from(session)
        if device_hash:
            customer = session.get("customer") if isinstance(session.get("customer"), str) else None
            sub_id = session.get("subscription") if isinstance(session.get("subscription"), str) else None
            if not sub_id:
                return {"action": "linked", "device": True}
            try:
                sub = self._fetch_subscription(sub_id)
            except Exception as exc:
                return {"action": "linked", "device": True, "reason": f"fetch_failed: {exc}"}
            tier, _status, period_end = self._tier_for(sub)
            if tier == "free":
                return {"action": "linked", "device": True}  # wrong SKU/inactive -> no license
            res = self.store.issue_license(
                stripe_checkout_session_id=session.get("id"), stripe_customer_id=customer,
                stripe_subscription_id=sub_id, tier=tier, current_period_end=period_end, device_hash=device_hash,
            )
            return {"action": "granted" if not res.get("already_issued") else "linked", "device": True, "tier": tier}
        project_id = self._project_id_from(session)
        if not project_id:
            return {"action": "skipped", "reason": "no project_id"}
        customer = session.get("customer") if isinstance(session.get("customer"), str) else None
        sub_id = session.get("subscription") if isinstance(session.get("subscription"), str) else None
        if customer:
            self.store.set_stripe_customer(project_id=project_id, customer_id=customer)
        if not sub_id:
            return {"action": "linked", "project_id": project_id}
        try:
            sub = self._fetch_subscription(sub_id)
        except Exception as exc:
            # Couldn't verify yet — leave tier alone; the subscription.* event will set it.
            return {"action": "linked", "project_id": project_id, "reason": f"fetch_failed: {exc}"}
        res = self._apply_subscription_obj(project_id, sub, customer)
        return {"action": "granted" if res["tier"] != "free" else "linked", "project_id": project_id, **res}

    def _on_subscription_change(self, sub_obj: dict[str, Any]) -> dict[str, Any]:
        sub_id = sub_obj.get("id")
        # Re-fetch the authoritative subscription: makes the grant independent of event ORDER
        # (a stale 'active' redelivered after cancel re-fetches and sees the real 'canceled')
        # and lets us verify the PRICE.
        sub = sub_obj
        if sub_id:
            try:
                sub = self._fetch_subscription(sub_id)
            except Exception as exc:
                return {"action": "deferred", "reason": f"fetch_failed: {exc}"}
        # Device subscription -> sync device licenses (period/status), never the project tier.
        if self._device_hash_from(sub) or self._device_hash_from(sub_obj):
            _tier, status, period_end = self._tier_for(sub)
            n = self.store.sync_licenses_for_subscription(subscription_id=sub_id, status=sub.get("status"), current_period_end=period_end)
            return {"action": "synced", "device": True, "licenses": n}
        project_id = self._project_id_from(sub_obj)
        if not project_id:
            return {"action": "skipped", "reason": "no project_id"}
        customer = sub.get("customer") if isinstance(sub.get("customer"), str) else None
        res = self._apply_subscription_obj(project_id, sub, customer)
        return {"action": "synced", "project_id": project_id, **res}

    def _on_subscription_deleted(self, sub: dict[str, Any]) -> dict[str, Any]:
        # Deletion is terminal and unambiguous — revoke directly without a re-fetch.
        if self._device_hash_from(sub):
            n = self.store.revoke_licenses_for_subscription(sub.get("id"))
            return {"action": "revoked", "device": True, "licenses": n}
        project_id = self._project_id_from(sub)
        if not project_id:
            return {"action": "skipped", "reason": "no project_id"}
        self.store.apply_subscription(
            project_id=project_id, tier="free", status="canceled",
            stripe_subscription_id=sub.get("id"),
        )
        return {"action": "revoked", "project_id": project_id, "tier": "free"}
