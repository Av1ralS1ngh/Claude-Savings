from __future__ import annotations

"""Device-licensing resolver for the request path (the BYOK/self-hosted monetization axis).

For a client-supplied device fingerprint, decides whether compression stays ON (an active Pro
license, or free daily quota still available) or degrades to OFF (free quota spent — the request
still reaches Claude, just uncompressed). Hosted/metered (`dc_` api_key) callers are NEVER
device-gated; the request handler checks `not rate_limits._metered(principal)` before calling
resolve(), so the project-tier model and this device axis stay orthogonal.

Nothing here trusts client input for entitlement: the device fingerprint is HMAC-hashed
server-side, and Pro comes only from a license row issued by a signature-verified Stripe path.

resolve() returns a decision dict that carries both the legacy fields (tier/compress/...) and
the richer license contract the desktop UI consumes: a full header set (decision_headers) and a
modal metadata block (ui_metadata) so a client like LicenseGate can render the upgrade prompt.
"""

import datetime as _dt
import logging
import urllib.parse
from typing import Any

# The CLI/SDK stamps a device fingerprint on every request. Primary header is
# x-decompute-device (what our CLI sends); we also accept the desktop-app aliases.
DEVICE_HEADER = "x-decompute-device"
DEVICE_HEADERS = (
    "x-decompute-device-fingerprint",
    "x-decompute-device",
    "x-decompute-device-id",
    "x-decompute-machine-id",
)

log = logging.getLogger("decompute.gateway.licensing")


def device_fingerprint_from_request(headers: Any, body: dict[str, Any] | None = None) -> str | None:
    """Extract the raw device fingerprint from any accepted header or the request body.

    `headers` is anything with .get() (a Starlette Headers or a plain dict). The body fallbacks
    let SDKs that can't set headers pass it under decompute.device_fingerprint / license.device_id.
    """
    for h in DEVICE_HEADERS:
        v = headers.get(h)
        if v and str(v).strip():
            return str(v).strip()
    body = body or {}
    for section in ("decompute", "license"):
        sec = body.get(section)
        if isinstance(sec, dict):
            for k in ("device_fingerprint", "device_id"):
                v = sec.get(k)
                if v and str(v).strip():
                    return str(v).strip()
    return None


class LicenseService:
    def __init__(self, settings: Any, store: Any):
        self.settings = settings
        self.store = store

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "device_licensing_enabled", True))

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve(self, device_fingerprint: str | None, *, consume: bool = True) -> dict[str, Any] | None:
        """Resolve a device's compression entitlement.

        Returns None when licensing doesn't apply (disabled, or no fingerprint -> ungated, which
        keeps local/dev/no-header traffic working). Otherwise a decision dict (see module docstring).
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

    def _free_limit(self) -> int:
        return int(getattr(self.settings, "free_compression_quota", 100))

    def _reset_at_iso(self) -> str:
        """Next free-quota reset (the daily window rolls at UTC midnight)."""
        now = _dt.datetime.now(_dt.timezone.utc)
        nxt = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return nxt.isoformat().replace("+00:00", "Z")

    def upgrade_url(self, device_hash: str | None = None) -> str:
        base = getattr(self.settings, "pro_upgrade_url", "https://decompute.ai/pro")
        params = {"plan": "pro"}
        if device_hash:
            params["device"] = device_hash[:32]
        rt = getattr(self.settings, "pro_upgrade_return_to", None)
        if rt:
            params["return_to"] = rt
        sep = "&" if "?" in base else "?"
        return base + sep + urllib.parse.urlencode(params)

    def _resolve(self, device_fingerprint: str, *, consume: bool) -> dict[str, Any]:
        device_hash = self.store.hash_device_fingerprint(device_fingerprint)
        limit = self._free_limit()
        upgrade_url = self.upgrade_url(device_hash)
        lic = self.store.get_active_license_for_device(device_hash)
        if lic:
            return {
                # legacy fields (kept for existing callers/tests)
                "tier": lic.get("tier", "pro"), "license_status": "active", "compress": True,
                "quota_used": None, "quota_remaining": None, "free_quota_limit": None,
                "device_hash_prefix": device_hash[:12],
                # contract fields
                "plan": "pro", "compression_allowed": True, "reason": "pro_license_active",
                "free_limit": limit, "free_used": 0, "free_remaining": limit,
                "reset_at_iso": None, "paused_until_iso": None, "upgrade_url": upgrade_url,
                "message": "Pro is active. Compression is unlimited under fair use for this device.",
            }
        if consume:
            q = self.store.try_consume_free_quota(device_hash)
            compress = bool(q["consumed"])
        else:
            q = self.store.get_device_quota(device_hash)
            compress = q["remaining"] > 0
        reset_at = self._reset_at_iso()
        if compress:
            status, reason = "free", "free_quota_available"
            paused_until = None
            message = f"Free compression is active. {q['remaining']} requests remain in this window."
        else:
            status, reason = "paused", "free_quota_exhausted"
            paused_until = reset_at  # compression resumes when the daily window resets
            message = ("You reached the free compression limit. Claude keeps working normally, but "
                       "Decompute compression is paused until the reset time. Upgrade to Pro for "
                       "unlimited fair-use compression on this device.")
        return {
            "tier": "free", "license_status": status, "compress": compress,
            "quota_used": q["used"], "quota_remaining": q["remaining"], "free_quota_limit": q["limit"],
            "device_hash_prefix": device_hash[:12],
            "plan": "free", "compression_allowed": compress, "reason": reason,
            "free_limit": q["limit"], "free_used": q["used"], "free_remaining": q["remaining"],
            "reset_at_iso": reset_at, "paused_until_iso": paused_until, "upgrade_url": upgrade_url,
            "message": message,
        }

    # ── response contract (headers + modal metadata) ──────────────────────────
    @staticmethod
    def decision_headers(meta: dict[str, Any] | None) -> dict[str, str]:
        if not meta:
            return {}
        return {
            "x-decompute-license-plan": str(meta.get("plan", "free")),
            "x-decompute-license-status": str(meta.get("license_status", "none")),
            "x-decompute-compression-allowed": str(bool(meta.get("compression_allowed"))).lower(),
            "x-decompute-compression-paused": str(not bool(meta.get("compression_allowed"))).lower(),
            "x-decompute-quota-free-limit": str(meta.get("free_limit", "")),
            "x-decompute-quota-free-used": str(meta.get("free_used", "")),
            "x-decompute-quota-free-remaining": str(meta.get("free_remaining", "")),
            "x-decompute-quota-reset-at": str(meta.get("reset_at_iso") or ""),
            "x-decompute-compression-paused-until": str(meta.get("paused_until_iso") or ""),
            "x-decompute-upgrade-url": str(meta.get("upgrade_url", "")),
            "x-decompute-license-reason": str(meta.get("reason", "")),
        }

    @staticmethod
    def ui_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
        """The `decompute.license` block a client UI reads to render the upgrade modal."""
        if not meta:
            return {}
        allowed = bool(meta.get("compression_allowed"))
        ui: dict[str, Any] = {
            "show_modal": not allowed,
            "title": "Decompute compression active" if allowed else "Free compression limit reached",
            "message": meta.get("message", ""),
            "actions": [] if allowed else [
                {"id": "continue_without_pro", "label": "Continue without Pro", "kind": "secondary",
                 "description": "Claude keeps working normally; compression stays paused until the reset time."},
                {"id": "continue_with_pro", "label": "Continue with Pro", "kind": "primary",
                 "url": meta.get("upgrade_url", ""),
                 "description": "Upgrade to Pro for unlimited fair-use compression on this device."},
            ],
        }
        return {
            "plan": meta.get("plan", "free"),
            "license_status": meta.get("license_status", "none"),
            "compression_allowed": allowed,
            "reason": meta.get("reason", ""),
            "quota": {"limit": meta.get("free_limit"), "used": meta.get("free_used"),
                      "remaining": meta.get("free_remaining"), "reset_at": meta.get("reset_at_iso")},
            "paused_until": meta.get("paused_until_iso"),
            "upgrade_url": meta.get("upgrade_url", ""),
            "ui": ui,
        }
