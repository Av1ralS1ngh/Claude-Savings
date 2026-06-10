"""Edge-case coverage for the offline signed-license verifier (the local-engine Pro gate).

Every REASON_* code has a test, plus the happy path. The guiding invariant: verify_license never
raises and only returns valid=True for a signature that was minted by the matching private key,
for THIS device, inside the validity window.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json

import pytest

from decompute_gateway import signed_license as sl


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def keypair():
    priv, pub = sl.generate_keypair()
    return priv, pub, sl.key_id(pub)


@pytest.fixture
def device_id():
    return sl.canonical_device_id("dev-abc123")


def _mint(keypair, device_id, *, expires_in_days: int | None = 30, tier="pro",
          issued_offset_days: float = 0.0, max_devices=1, subject=None):
    priv, _pub, kid = keypair
    now = _dt.datetime(2026, 6, 10, tzinfo=_dt.timezone.utc)
    issued = now + _dt.timedelta(days=issued_offset_days)
    expires = None if expires_in_days is None else issued + _dt.timedelta(days=expires_in_days)
    return sl.sign_license(
        private_pem=priv, kid=kid, license_id="lic_test", device_id=device_id,
        issued_at=issued, expires_at=expires, tier=tier, max_devices=max_devices, subject=subject,
    ), now


def _keys(keypair):
    _priv, pub, kid = keypair
    return {kid: pub}


# ── happy path ────────────────────────────────────────────────────────────────
def test_valid_license_grants_pro(keypair, device_id):
    text, now = _mint(keypair, device_id)
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=now)
    assert v.valid and v.reason == sl.REASON_OK
    assert v.tier == "pro" and v.license_id == "lic_test"


def test_perpetual_license_never_expires(keypair, device_id):
    text, _now = _mint(keypair, device_id, expires_in_days=None)
    far_future = _dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=far_future)
    assert v.valid and v.expires_at is None


def test_subject_roundtrips(keypair, device_id):
    text, now = _mint(keypair, device_id, subject="buyer@example.com")
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=now)
    assert v.valid and v.payload["subject"] == "buyer@example.com"


# ── forgery / tamper ──────────────────────────────────────────────────────────
def test_tampered_payload_fails_signature(keypair, device_id):
    text, now = _mint(keypair, device_id, tier="pro")
    envelope = json.loads(sl._b64u_decode(text))
    payload = json.loads(sl._b64u_decode(envelope["payload_b64"]))
    payload["tier"] = "enterprise"  # privilege-escalation attempt
    envelope["payload_b64"] = sl._b64u_encode(json.dumps(payload).encode())
    forged = sl._b64u_encode(json.dumps(envelope).encode())
    v = sl.verify_license(forged, device_id=device_id, public_keys=_keys(keypair), now=now)
    assert not v.valid and v.reason == sl.REASON_BAD_SIGNATURE


def test_license_signed_by_a_different_key_is_rejected(keypair, device_id):
    # Attacker generates their OWN keypair and signs a Pro license, but ships under the real kid.
    attacker_priv, _attacker_pub = sl.generate_keypair()
    _priv, _pub, real_kid = keypair
    now = _dt.datetime(2026, 6, 10, tzinfo=_dt.timezone.utc)
    forged = sl.sign_license(private_pem=attacker_priv, kid=real_kid, license_id="lic_x",
                             device_id=device_id, issued_at=now, expires_at=now + _dt.timedelta(days=9999))
    v = sl.verify_license(forged, device_id=device_id, public_keys=_keys(keypair), now=now)
    assert not v.valid and v.reason == sl.REASON_BAD_SIGNATURE


def test_unknown_kid_is_rejected(keypair, device_id):
    text, now = _mint(keypair, device_id)
    v = sl.verify_license(text, device_id=device_id, public_keys={"someone-elses-kid": keypair[1]}, now=now)
    assert not v.valid and v.reason == sl.REASON_UNKNOWN_KID


# ── device binding ────────────────────────────────────────────────────────────
def test_license_for_another_machine_is_rejected(keypair, device_id):
    text, now = _mint(keypair, device_id)
    other = sl.canonical_device_id("dev-someone-else")
    v = sl.verify_license(text, device_id=other, public_keys=_keys(keypair), now=now)
    assert not v.valid and v.reason == sl.REASON_WRONG_DEVICE


def test_canonical_device_id_is_stable_and_salt_free():
    # Same raw fingerprint -> same id on any machine (no per-deployment secret involved).
    assert sl.canonical_device_id("dev-abc123") == sl.canonical_device_id(" dev-abc123 ")
    assert sl.canonical_device_id("a") != sl.canonical_device_id("b")


# ── validity window ───────────────────────────────────────────────────────────
def test_expired_license_is_rejected(keypair, device_id):
    text, now = _mint(keypair, device_id, expires_in_days=30)
    later = now + _dt.timedelta(days=31)
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=later)
    assert not v.valid and v.reason == sl.REASON_EXPIRED


def test_expiry_grace_window_keeps_pro_briefly(keypair, device_id):
    text, now = _mint(keypair, device_id, expires_in_days=30)
    just_after = now + _dt.timedelta(days=31)
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=just_after, grace_days=3)
    assert v.valid and v.reason == sl.REASON_OK


def test_not_yet_valid_license_is_rejected(keypair, device_id):
    # issued 10 days in the future relative to `now`.
    text, now = _mint(keypair, device_id, issued_offset_days=10)
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair), now=now)
    assert not v.valid and v.reason == sl.REASON_NOT_YET_VALID


def test_clock_skew_tolerates_small_forward_drift(keypair, device_id):
    text, now = _mint(keypair, device_id, issued_offset_days=0)
    slightly_behind = now - _dt.timedelta(seconds=120)  # local clock 2 min slow
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair),
                          now=slightly_behind, clock_skew_s=300)
    assert v.valid


# ── malformed / empty / inert ─────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_is_not_pro(bad, keypair, device_id):
    v = sl.verify_license(bad, device_id=device_id, public_keys=_keys(keypair))
    assert not v.valid and v.reason == sl.REASON_EMPTY


@pytest.mark.parametrize("bad", [
    "not-base64-!!!@@@",
    "YWJjZA",                                               # valid b64 but not JSON
    base64.urlsafe_b64encode(b'{"v":1}').decode(),          # JSON envelope missing kid/payload/sig
])
def test_malformed_inputs_are_rejected_not_crashing(bad, keypair, device_id):
    v = sl.verify_license(bad, device_id=device_id, public_keys=_keys(keypair))
    assert not v.valid and v.reason == sl.REASON_MALFORMED


def test_known_kid_but_junk_payload_fails_at_signature(keypair, device_id):
    # A well-formed envelope under the REAL kid, but garbage payload/sig bytes -> caught by the
    # signature check (never trusted as a license), not a crash.
    _priv, _pub, kid = keypair
    envelope = {"v": 1, "kid": kid, "payload_b64": sl._b64u_encode(b"{}"), "sig_b64": sl._b64u_encode(b"\x00" * 64)}
    text = sl._b64u_encode(json.dumps(envelope).encode())
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair))
    assert not v.valid and v.reason == sl.REASON_BAD_SIGNATURE


def test_unknown_format_version_is_rejected(keypair, device_id):
    envelope = {"v": 999, "kid": "x", "payload_b64": "AA", "sig_b64": "AA"}
    text = sl._b64u_encode(json.dumps(envelope).encode())
    v = sl.verify_license(text, device_id=device_id, public_keys=_keys(keypair))
    assert not v.valid and v.reason == sl.REASON_UNKNOWN_VERSION


def test_no_public_key_makes_path_inert(keypair, device_id):
    text, now = _mint(keypair, device_id)
    v = sl.verify_license(text, device_id=device_id, public_keys={}, now=now)
    assert not v.valid and v.reason == sl.REASON_NO_PUBLIC_KEY


# ── key + resolve helpers ─────────────────────────────────────────────────────
def test_key_id_is_deterministic_per_key(keypair):
    _priv, pub, kid = keypair
    assert sl.key_id(pub) == kid and len(kid) == 16


def test_resolve_public_keys_merges_env_json_over_embedded():
    class S:
        license_public_keys = {"a": "PEM_A"}
        license_public_keys_json = json.dumps({"b": "PEM_B"})
    keys = sl.resolve_public_keys(S())
    assert keys["a"] == "PEM_A" and keys["b"] == "PEM_B"


def test_sign_rejects_invalid_tier(keypair, device_id):
    priv, _pub, kid = keypair
    now = _dt.datetime(2026, 6, 10, tzinfo=_dt.timezone.utc)
    with pytest.raises(ValueError):
        sl.sign_license(private_pem=priv, kid=kid, license_id="x", device_id=device_id,
                        issued_at=now, expires_at=None, tier="megacorp")
