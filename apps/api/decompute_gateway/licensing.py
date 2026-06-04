from __future__ import annotations

"""Device-licensing resolver for the request path (the BYOK/self-hosted monetization axis).

For a client-supplied device fingerprint, decides whether compression stays ON (an active Pro
license, or free daily quota still available) or degrades to OFF (free quota spent — the request
still reaches Claude, just uncompressed). Hosted/metered (`dc_` api_key) callers are NEVER
device-gated; the request handler checks `not rate_limits._metered(principal)` before calling
resolve(), so the project-tier model and this device axis stay orthogonal.

Nothing here trusts client input for entitlement: the device fingerprint is HMAC-hashed
server-side, and Pro comes only from a license row issued by a signature-verified Stripe path.
"""

import logging
from typing import Any

# Header the CLI/SDK stamps on every request (mirrors x-decompute-session-id).
DEVICE_HEADER = "x-decompute-device"

log = logging.getLogger("decompute.gateway.licensing")


class LicenseService:
    def __init__(self, settings: Any, store: Any):
        self.settings = settings
        self.store = store

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "device_licensing_enabled", True))

    def resolve(self, device_fingerprint: str | None, *, consume: bool = True) -> dict[str, Any] | None:
        """Resolve a device's compression entitlement.

        Returns None when licensing doesn't apply (disabled, or no fingerprint -> ungated, which
        keeps local/dev/no-header traffic working). Otherwise returns:
          {tier, license_status, compress(bool), quota_used, quota_remaining, free_quota_limit,
           device_hash_prefix}
        With consume=True (the request path) it atomically spends one free-tier unit when on the
        free tier; with consume=False (status reads) it only inspects.
        """
        if not self.enabled or not device_fingerprint:
            return None
        try:
            return self._resolve(device_fingerprint, consume=consume)
        except Exception as exc:
            # FAIL-OPEN: device licensing must never break the core proxy. A licensing-store
            # error (e.g. migration not yet applied) leaves the request ungated — compression
            # proceeds normally rather than 500-ing the user's request.
            log.warning("device-licensing resolve failed; treating request as ungated: %s", exc)
            return None

    def _resolve(self, device_fingerprint: str, *, consume: bool) -> dict[str, Any] | None:
        device_hash = self.store.hash_device_fingerprint(device_fingerprint)
        lic = self.store.get_active_license_for_device(device_hash)
        if lic:
            return {
                "tier": lic.get("tier", "pro"), "license_status": "active", "compress": True,
                "quota_used": None, "quota_remaining": None, "free_quota_limit": None,
                "device_hash_prefix": device_hash[:12],
            }
        if consume:
            q = self.store.try_consume_free_quota(device_hash)
            compress = bool(q["consumed"])
        else:
            q = self.store.get_device_quota(device_hash)
            compress = q["remaining"] > 0
        return {
            "tier": "free", "license_status": "none", "compress": compress,
            "quota_used": q["used"], "quota_remaining": q["remaining"], "free_quota_limit": q["limit"],
            "device_hash_prefix": device_hash[:12],
        }
