"""SSRF + trust-tier classification for student inference endpoints (V1a foundation).

Security-critical: the gateway will route a user's (possibly proprietary) prompts to a configured
endpoint URL, so the classifier must (1) NEVER allow cloud-metadata / link-local / reserved ranges
(SSRF), and (2) tier loopback vs private(LAN/VPN) vs public(third-party) correctly, with public gated
behind explicit consent + TLS."""
from __future__ import annotations

import pytest

from decompute_gateway.endpoint_security import (
    TIER_BLOCKED,
    TIER_LOOPBACK,
    TIER_PRIVATE,
    TIER_PUBLIC,
    classify_endpoint,
    validate_student_endpoint,
)


# ── SSRF: these must ALWAYS be blocked, regardless of policy flags ──────────────
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",     # AWS/GCP cloud metadata — the classic SSRF target
    "http://169.254.169.254:8080/v1",
    "http://[fe80::1]/v1",                           # IPv6 link-local
    "http://224.0.0.1/v1",                           # multicast
    "http://0.0.0.0/v1",                             # unspecified... (see note below)
])
def test_blocked_ranges_are_rejected(url):
    v = classify_endpoint(url)
    # 0.0.0.0 classifies as unspecified->blocked; metadata/link-local/multicast all blocked.
    assert v.tier == TIER_BLOCKED and not v.ok, f"{url} should be blocked, got {v.tier}"


def test_metadata_ip_blocked_even_with_all_flags_on():
    """Even with allow_private + allow_public, the metadata IP is never callable."""
    v = validate_student_endpoint("http://169.254.169.254/v1", allow_private=True, allow_public=True,
                                  require_tls_public=False)
    assert not v.ok and v.tier == TIER_BLOCKED


def test_non_http_scheme_rejected():
    assert not classify_endpoint("file:///etc/passwd").ok
    assert not classify_endpoint("gopher://x/").ok
    assert not classify_endpoint("ftp://host/").ok


def test_no_host_rejected():
    assert not classify_endpoint("http:///v1").ok


# ── Tier classification ─────────────────────────────────────────────────────────
def test_loopback_tier():
    for url in ("http://127.0.0.1:18080/v1", "http://localhost:8080/v1", "http://[::1]/v1"):
        v = classify_endpoint(url)
        assert v.ok and v.tier == TIER_LOOPBACK, url


def test_private_lan_tier():
    for url in ("http://192.168.1.50:8080/v1", "http://10.0.0.5/v1", "http://172.16.3.4/v1"):
        v = classify_endpoint(url)
        assert v.ok and v.tier == TIER_PRIVATE, url


def test_tailscale_cgnat_is_private():
    v = classify_endpoint("http://100.101.102.103:8080/v1")  # 100.64.0.0/10 (Tailscale)
    assert v.ok and v.tier == TIER_PRIVATE


def test_public_tier():
    v = classify_endpoint("https://1.1.1.1/v1")  # a routable public IP literal
    assert v.ok and v.tier == TIER_PUBLIC


# ── Policy gate (validate_student_endpoint) ─────────────────────────────────────
def test_loopback_always_allowed():
    v = validate_student_endpoint("http://127.0.0.1:18080/v1", allow_private=False, allow_public=False)
    assert v.ok and v.tier == TIER_LOOPBACK


def test_private_requires_allow_private():
    url = "http://192.168.1.50:8080/v1"
    assert not validate_student_endpoint(url, allow_private=False).ok
    assert validate_student_endpoint(url, allow_private=True).ok


def test_public_requires_consent_and_tls():
    pub = "https://1.1.1.1/v1"
    # no consent -> rejected
    assert not validate_student_endpoint(pub, allow_public=False).ok
    assert validate_student_endpoint(pub, allow_public=False).reason == "public_endpoint_requires_consent"
    # consent given, https -> ok
    assert validate_student_endpoint(pub, allow_public=True).ok
    # consent given but http (not https) -> rejected
    assert not validate_student_endpoint("http://1.1.1.1/v1", allow_public=True, require_tls_public=True).ok
    assert validate_student_endpoint("http://1.1.1.1/v1", allow_public=True, require_tls_public=True).reason == "public_endpoint_requires_https"


def test_public_requires_consent_property():
    v = classify_endpoint("https://1.1.1.1/v1")
    assert v.requires_consent is True
    assert classify_endpoint("http://127.0.0.1/v1").requires_consent is False
