"""Trust classification + SSRF / exfiltration guard for a student inference endpoint URL.

The teacher-student feature is generalizing from "a single local llama-server" to "any OpenAI-compatible
student endpoint" (a bigger local box, a LAN/Tailscale machine, a private/hosted GPU). That opens a real
attack/trust surface, so every backend's preflight runs `validate_student_endpoint()` before the gateway
will route a user's (possibly proprietary) prompts to an endpoint. It answers two questions:

  1. SSRF — is this URL safe to call AT ALL? A user-supplied URL must never be allowed to reach cloud
     metadata (169.254.169.254) or other link-local/reserved ranges, regardless of trust tier.
  2. TRUST TIER — loopback (the local student, trusted), private (LAN/VPN cluster), or public (a
     third-party endpoint — a trust-boundary change that requires https + explicit consent).

Design notes:
  * Hostnames are resolved and ALL resolved IPs are classified; if ANY resolves to a blocked range the
    URL is rejected (stops "innocent hostname -> metadata" tricks). The trust tier is the MOST PUBLIC of
    the resolved IPs (fail safe).
  * DNS-rebinding (resolve-public-then-rebind-to-metadata) is a known residual; pinning the resolved IP
    at call time is a documented future hardening. For V1a we classify at preflight + reject blocked.
  * This module has no I/O beyond DNS resolution and no dependency on settings — it's a pure policy
    function so it's trivially testable and reusable (provider preflight, config validation, the CLI).
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Tiers, most-trusted -> least.
TIER_LOOPBACK = "loopback"   # 127.0.0.1 / ::1 / localhost — the local student
TIER_PRIVATE = "private"     # RFC-1918 / CGNAT(Tailscale) / unique-local IPv6 — a LAN/VPN cluster
TIER_PUBLIC = "public"       # anything routable on the internet — a third-party endpoint
TIER_BLOCKED = "blocked"     # never callable (SSRF): link-local/metadata, multicast, reserved


@dataclass(frozen=True)
class EndpointVerdict:
    ok: bool
    tier: str
    reason: str
    host: str = ""
    resolved_ips: list[str] = field(default_factory=list)

    @property
    def requires_consent(self) -> bool:
        """Public (third-party) endpoints are a trust-boundary change — off by default, opt-in only."""
        return self.tier == TIER_PUBLIC


def _classify_ip(ip: ipaddress._BaseAddress) -> str:
    """Map a single IP to a trust tier. Loopback is always safe; otherwise blocked ranges win."""
    # Loopback (127.0.0.1 / ::1) is the local student — unambiguously safe, checked first (some Python
    # versions also flag ::1 as reserved, which must not block it).
    if ip.is_loopback:
        return TIER_LOOPBACK
    # Always-blocked: link-local (incl. cloud metadata 169.254.169.254 + fe80::/10), multicast,
    # unspecified, reserved. These must never be reachable through the gateway.
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return TIER_BLOCKED
    # Tailscale CGNAT range 100.64.0.0/10 is "private" for our purposes (a VPN cluster link).
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return TIER_PRIVATE
    if ip.is_private:
        return TIER_PRIVATE
    return TIER_PUBLIC


_TIER_RANK = {TIER_LOOPBACK: 0, TIER_PRIVATE: 1, TIER_PUBLIC: 2, TIER_BLOCKED: 3}


def _resolve(host: str) -> list[str]:
    """Resolve a hostname to all its IPs. An IP literal returns itself. Empty list on failure."""
    try:
        ipaddress.ip_address(host)
        return [host]  # already an IP literal
    except ValueError:
        pass
    if host.lower() == "localhost":
        return ["127.0.0.1"]
    try:
        infos = socket.getaddrinfo(host, None)
        return sorted({info[4][0] for info in infos})
    except (socket.gaierror, OSError):
        return []


def classify_endpoint(url: str) -> EndpointVerdict:
    """Classify a student endpoint URL into a trust tier, resolving the host and checking every IP.

    Returns a BLOCKED verdict (ok=False) for an unparseable URL, a non-http(s) scheme, an unresolvable
    host, or any host that resolves into a blocked (SSRF) range."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return EndpointVerdict(False, TIER_BLOCKED, "scheme_not_http", host=parsed.hostname or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return EndpointVerdict(False, TIER_BLOCKED, "no_host")

    ips = _resolve(host)
    if not ips:
        return EndpointVerdict(False, TIER_BLOCKED, "host_unresolvable", host=host)

    tiers = [_classify_ip(ipaddress.ip_address(ip)) for ip in ips]
    if TIER_BLOCKED in tiers:
        # Any resolved IP in a blocked range => reject the whole URL (anti-rebinding-to-metadata).
        return EndpointVerdict(False, TIER_BLOCKED, "resolves_to_blocked_range", host=host, resolved_ips=ips)
    # Trust tier = the MOST PUBLIC resolved IP (fail safe: a host that resolves to both private+public
    # is treated as public, requiring consent/TLS).
    tier = max(tiers, key=lambda t: _TIER_RANK[t])
    return EndpointVerdict(True, tier, "ok", host=host, resolved_ips=ips)


def validate_student_endpoint(
    url: str,
    *,
    allow_private: bool = True,
    allow_public: bool = False,
    require_tls_public: bool = True,
) -> EndpointVerdict:
    """Policy gate every backend preflight runs before routing prompts to `url`.

    - Always rejects SSRF (blocked ranges) and non-http(s).
    - loopback: allowed (the local student).
    - private (LAN/Tailscale): allowed only when `allow_private` (a cluster/LAN opt-in).
    - public (third-party): allowed only when `allow_public` (explicit consent — a trust-boundary
      change) AND, when `require_tls_public`, only over https (don't ship plaintext prompts to the
      internet).
    The returned verdict's `ok` reflects the policy decision; `tier`/`requires_consent` let the caller
    surface the right message + consent prompt."""
    v = classify_endpoint(url)
    if not v.ok:
        return v  # SSRF / unparseable — hard no
    if v.tier == TIER_LOOPBACK:
        return v
    if v.tier == TIER_PRIVATE:
        if not allow_private:
            return EndpointVerdict(False, v.tier, "private_endpoint_not_allowed", v.host, v.resolved_ips)
        return v
    # public
    if not allow_public:
        return EndpointVerdict(False, v.tier, "public_endpoint_requires_consent", v.host, v.resolved_ips)
    if require_tls_public and urlparse(url).scheme != "https":
        return EndpointVerdict(False, v.tier, "public_endpoint_requires_https", v.host, v.resolved_ips)
    return v
