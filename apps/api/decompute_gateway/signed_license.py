from __future__ import annotations

"""Offline, cryptographically-signed device licenses (the local-engine monetization axis).

The compression engine runs on the USER'S machine, so the Pro/Free decision is made locally and
CANNOT trust a local database row (a user could just insert one — see db.get_active_license_for_device).
Instead, Pro is proven by a LICENSE FILE that the hosted issuer signs with an Ed25519 PRIVATE key
that never leaves Decompute. The local engine ships only the matching PUBLIC key and verifies the
signature offline — a user cannot forge a license without the private key.

Obfuscation (Nuitka) and this signature are COMPLEMENTARY, not redundant:
  * the signature stops FORGERY (you can't mint a license without the private key);
  * Nuitka raises the cost of PATCHING THE CHECK OUT (deleting the call to verify_license()).
Neither alone is a hard boundary; together they make casual piracy impractical, which is the
realistic goal for client-side licensing.

Threat model:
  forge a Pro license .............. STOPPED  (needs the private key)
  copy one license to N machines ... STOPPED  (bound to a per-machine device id; max_devices)
  keep Pro after the sub lapses ..... STOPPED at the signed expiry (offline => only at expiry)
  patch verify_license() out ........ RAISED, not stopped (Nuitka's job; defense in depth)

File format (a single line of text, default ~/.decompute/license.dcmp):

    base64url( envelope_json )

  envelope = {"v": 1, "kid": "<key id>", "payload_b64": "<b64url>", "sig_b64": "<b64url>"}

The signature is over the EXACT payload bytes carried in payload_b64 — we verify those bytes
verbatim and never re-serialize the payload first, so verification has zero dependency on
canonical-JSON key ordering (a classic source of "valid signature won't verify" bugs).

  payload = {
    "v": 1, "license_id": "lic_…", "device_id": "<sha256(raw_fingerprint)>",
    "tier": "pro", "issued_at": "<iso8601 Z>", "expires_at": "<iso8601 Z>"|null,
    "max_devices": 1, "subject": "<email/customer, optional>"
  }
"""

import base64
import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

FORMAT_VERSION = 1
VALID_TIERS = frozenset({"pro", "team", "enterprise"})

# Embedded trusted public keys ({kid: PEM}). At runtime, settings.license_public_keys (env
# DECOMPUTE_LICENSE_PUBLIC_KEYS) is merged OVER this. The PUBLIC key is safe to ship/commit — only the
# matching PRIVATE key (in your secret manager) can sign a license. When the engine is Nuitka-compiled
# this constant is baked into the native .so, so a user can't swap in their own verify key.
EMBEDDED_LICENSE_PUBLIC_KEYS: dict[str, str] = {
    # Production verify key — kid 1bb67b036447838e. Private half: ~/decompute-signing/license_private.pem
    # (move to a secret manager; never ship/commit it).
    "1bb67b036447838e": (
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEA9FHDTwStFSMvBS2n8kpGyg1BqGm5oMQlwJiItX0EA1E=\n"
        "-----END PUBLIC KEY-----\n"
    ),
}


# ── encoding helpers (unpadded base64url everywhere) ──────────────────────────
def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    # Re-pad to a multiple of 4 before decoding; reject non-ascii up front.
    s = text.encode("ascii")  # raises UnicodeEncodeError on non-ascii -> caller treats as malformed
    pad = b"=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── device identity ───────────────────────────────────────────────────────────
def canonical_device_id(raw_fingerprint: str) -> str:
    """A stable, salt-free machine id that the issuer AND the local verifier compute IDENTICALLY
    from the same raw device fingerprint (e.g. the per-machine UUID in ~/.decompute/device.id).

    Deliberately distinct from db.hash_device_fingerprint, which HMACs with a per-deployment pepper
    for the server DB axis. A license must bind the same way on every deployment, so it cannot
    depend on a secret salt — the binding's job is "same machine", not "hide the fingerprint".
    """
    return hashlib.sha256(raw_fingerprint.strip().encode("utf-8")).hexdigest()


# ── key management (issuer side) ──────────────────────────────────────────────
def generate_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for a fresh Ed25519 signing key. Run ONCE; keep the
    private PEM in a secret manager (never commit, never ship). The public PEM is embedded in the
    engine build / set via DECOMPUTE_LICENSE_PUBLIC_KEYS."""
    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def key_id(public_pem: str) -> str:
    """A short, stable id for a public key (first 16 hex of sha256 of its DER bytes). Lets a
    license name WHICH key signed it, so keys can be rotated without breaking old licenses."""
    pub = load_public_key(public_pem)
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def load_private_key(private_pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    return key


def load_public_key(public_pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return key


def public_pem_from_private(private_pem: str) -> str:
    """Derive the public PEM from a private PEM (issuer convenience: kid + verify-key from one file)."""
    pub = load_private_key(private_pem).public_key()
    return pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


# ── signing (issuer side; needs the PRIVATE key) ──────────────────────────────
def _iso(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sign_license(
    *,
    private_pem: str,
    kid: str,
    license_id: str,
    device_id: str,
    issued_at: _dt.datetime,
    expires_at: _dt.datetime | None,
    tier: str = "pro",
    max_devices: int = 1,
    subject: str | None = None,
) -> str:
    """Produce the signed license-file text. `device_id` MUST be canonical_device_id(raw_fp) of the
    buyer's machine. `expires_at=None` mints a perpetual license (use for lifetime deals only)."""
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(VALID_TIERS)}")
    payload = {
        "v": FORMAT_VERSION,
        "license_id": license_id,
        "device_id": device_id,
        "tier": tier,
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at) if expires_at is not None else None,
        "max_devices": int(max_devices),
    }
    if subject:
        payload["subject"] = subject
    # Sign the EXACT bytes we transmit (sort_keys for reproducible issuance; verify never re-serializes).
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = load_private_key(private_pem).sign(payload_bytes)
    envelope = {
        "v": FORMAT_VERSION,
        "kid": kid,
        "payload_b64": _b64u_encode(payload_bytes),
        "sig_b64": _b64u_encode(signature),
    }
    envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return _b64u_encode(envelope_bytes)


# ── verifying (engine side; needs only the PUBLIC key) ────────────────────────
@dataclass
class LicenseVerdict:
    """Outcome of verifying a license file. `valid` gates Pro; `reason` is a stable machine code
    (see REASONS) for logging/telemetry; never raises — malformed input returns valid=False."""

    valid: bool
    reason: str
    tier: str | None = None
    license_id: str | None = None
    expires_at: str | None = None
    device_id: str | None = None
    payload: dict[str, Any] | None = field(default=None, repr=False)


# Stable reason codes (telemetry-safe; do not renumber):
REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN_VERSION = "unknown_version"
REASON_NO_PUBLIC_KEY = "no_public_key"
REASON_UNKNOWN_KID = "unknown_kid"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_WRONG_DEVICE = "wrong_device"
REASON_EXPIRED = "expired"
REASON_NOT_YET_VALID = "not_yet_valid"
REASON_BAD_TIER = "bad_tier"


def _parse_dt(value: str | None) -> _dt.datetime | None:
    if value is None:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)


def verify_license(
    text: str | None,
    *,
    device_id: str,
    public_keys: Mapping[str, str],
    now: _dt.datetime | None = None,
    clock_skew_s: int = 300,
    grace_days: int = 0,
) -> LicenseVerdict:
    """Verify a license-file string for THIS machine. Pure and total — any malformed/forged/foreign
    input yields valid=False with a reason, never an exception.

    Order is deliberate: structure -> key lookup -> SIGNATURE (before trusting any field) -> device
    binding -> validity window. We never read tier/expiry from an unverified payload.
    """
    if not text or not text.strip():
        return LicenseVerdict(False, REASON_EMPTY)
    if not public_keys:
        # Feature configured off / no trusted key shipped: signed-license path is simply inert.
        return LicenseVerdict(False, REASON_NO_PUBLIC_KEY)

    # 1. Envelope.
    try:
        envelope = json.loads(_b64u_decode(text.strip()))
        if not isinstance(envelope, dict):
            raise ValueError("envelope not an object")
        if int(envelope.get("v", 0)) != FORMAT_VERSION:
            return LicenseVerdict(False, REASON_UNKNOWN_VERSION)
        kid = str(envelope["kid"])
        payload_bytes = _b64u_decode(str(envelope["payload_b64"]))
        signature = _b64u_decode(str(envelope["sig_b64"]))
    except (ValueError, KeyError, TypeError, UnicodeEncodeError, json.JSONDecodeError, base64.binascii.Error):
        return LicenseVerdict(False, REASON_MALFORMED)

    # 2. Trusted key for this kid.
    pub_pem = public_keys.get(kid)
    if not pub_pem:
        return LicenseVerdict(False, REASON_UNKNOWN_KID)

    # 3. Signature FIRST — nothing in the payload is trusted until this passes.
    try:
        load_public_key(pub_pem).verify(signature, payload_bytes)
    except (InvalidSignature, ValueError):
        return LicenseVerdict(False, REASON_BAD_SIGNATURE)

    # 4. Payload is now authentic; parse it.
    try:
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict) or int(payload.get("v", 0)) != FORMAT_VERSION:
            return LicenseVerdict(False, REASON_MALFORMED)
        tier = str(payload.get("tier", ""))
        lic_device = str(payload.get("device_id", ""))
        license_id = str(payload.get("license_id", "")) or None
        issued_at = _parse_dt(payload.get("issued_at"))
        expires_at_raw = payload.get("expires_at")
        expires_at = _parse_dt(expires_at_raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return LicenseVerdict(False, REASON_MALFORMED)

    if tier not in VALID_TIERS:
        return LicenseVerdict(False, REASON_BAD_TIER, payload=payload)

    # 5. Device binding (constant-time compare to avoid leaking via timing; values are public hashes
    #    so this is belt-and-suspenders, but cheap and correct).
    if not _consteq(lic_device, device_id):
        return LicenseVerdict(False, REASON_WRONG_DEVICE, tier=tier, license_id=license_id, payload=payload)

    # 6. Validity window (skew tolerance forward, grace tolerance at expiry).
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    skew = _dt.timedelta(seconds=max(0, clock_skew_s))
    if issued_at is not None and now + skew < issued_at:
        return LicenseVerdict(False, REASON_NOT_YET_VALID, tier=tier, license_id=license_id,
                              expires_at=expires_at_raw, device_id=lic_device, payload=payload)
    if expires_at is not None and now - _dt.timedelta(days=max(0, grace_days)) > expires_at:
        return LicenseVerdict(False, REASON_EXPIRED, tier=tier, license_id=license_id,
                              expires_at=expires_at_raw, device_id=lic_device, payload=payload)

    return LicenseVerdict(True, REASON_OK, tier=tier, license_id=license_id,
                          expires_at=expires_at_raw, device_id=lic_device, payload=payload)


def _consteq(a: str, b: str) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def resolve_public_keys(settings: Any) -> dict[str, str]:
    """Merge env/settings-provided trusted keys over the embedded ones. settings may expose
    `license_public_keys` (a dict) or `license_public_keys_json` (a JSON string {kid: pem})."""
    keys = dict(EMBEDDED_LICENSE_PUBLIC_KEYS)
    explicit = getattr(settings, "license_public_keys", None)
    if isinstance(explicit, Mapping):
        keys.update({str(k): str(v) for k, v in explicit.items()})
    raw = getattr(settings, "license_public_keys_json", "") or ""
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                keys.update({str(k): str(v) for k, v in parsed.items()})
        except json.JSONDecodeError:
            pass
    return keys
