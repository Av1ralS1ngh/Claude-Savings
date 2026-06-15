from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import tomllib
from typing import Iterable, Literal

from fastapi import HTTPException, Request

from .config import Settings
from .db import Store


@dataclass(frozen=True)
class AuthPrincipal:
    project_id: str
    org_id: str | None
    key_id: str | None
    key_prefix: str | None
    scopes: list[str]
    auth_mode: str

    def has_scope(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    def require(self, *scopes: str) -> None:
        missing = [s for s in scopes if not self.has_scope(s)]
        if missing:
            raise HTTPException(status_code=403, detail={"error": "missing_scope", "required": missing})


@dataclass(frozen=True)
class UpstreamAnthropicAuth:
    kind: Literal["api_key", "bearer"]
    value: str


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def platform_api_key_from_request(request: Request) -> str | None:
    # Decompute platform auth intentionally does NOT use Anthropic x-api-key.
    dedicated = request.headers.get("x-decompute-api-key") or request.headers.get("x-decompute-key")
    if dedicated:
        return dedicated
    bearer = extract_bearer(request.headers.get("authorization"))
    if bearer and bearer.startswith("dc_"):
        return bearer
    return None


def auth_context(request: Request, store: Store, settings: Settings, *, required_scope: str | None = None) -> AuthPrincipal:
    raw = platform_api_key_from_request(request)

    if raw and raw == settings.dev_api_key and not settings.is_production:
        # Explicit dev convenience key — allowed only in non-production.
        principal = AuthPrincipal(
            project_id="proj_default",
            org_id="org_default",
            key_id=None,
            key_prefix=raw[:12],
            scopes=["admin"],
            auth_mode="dev",
        )
    elif raw:
        # A key was supplied — it must verify. Never fall through to local_noauth
        # when a key is present; a revoked or unknown key is always 401.
        found = store.verify_api_key(raw)
        if not found:
            raise HTTPException(status_code=401, detail="Invalid or revoked Decompute API key")
        principal = AuthPrincipal(
            project_id=found["project_id"],
            org_id=found.get("org_id"),
            key_id=found.get("key_id"),
            key_prefix=found.get("key_prefix"),
            scopes=found.get("scopes") or [],
            auth_mode="api_key",
        )
    elif settings.require_auth:
        # No key and auth is required.
        raise HTTPException(status_code=401, detail="Missing Decompute API key")
    else:
        # No key provided in local/dev mode — unauthenticated passthrough.
        principal = AuthPrincipal(
            project_id="proj_default",
            org_id="org_default",
            key_id=None,
            key_prefix=None,
            scopes=["admin"],
            auth_mode="local_noauth",
        )

    if required_scope:
        principal.require(required_scope)
    return principal


def _read_byok_key(settings: Settings) -> str | None:
    """The BYOK Claude key the user connected via `decompute init` (~/.decompute/claude.key).

    Read per-request so a key connected after the gateway started is picked up without a restart;
    the file is tiny and OS-cached, and the key never leaves ~/.decompute (it is NOT written into
    ~/.claude/settings.json)."""
    try:
        p = Path(settings.data_dir) / "claude.key"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def _local_claude_mode(settings: Settings) -> str:
    try:
        cfg = tomllib.loads((Path(settings.data_dir) / "config.toml").read_text(encoding="utf-8"))
        mode = ((cfg.get("claude") or {}).get("mode") or "").strip()
        return mode or "anthropic_api"
    except Exception:
        return "anthropic_api"


def upstream_anthropic_auth(request: Request, settings: Settings) -> UpstreamAnthropicAuth | None:
    if settings.anthropic_api_key:
        return UpstreamAnthropicAuth("api_key", settings.anthropic_api_key)
    # Local BYOK: the key connected via `decompute init`. Keep this ahead of client credentials so
    # existing API-key setups remain stable. The CLI/extension subscription mode will make this
    # precedence explicit to users instead of silently mixing auth modes.
    if _local_claude_mode(settings) != "claude_subscription":
        byok = _read_byok_key(settings)
        if byok:
            return UpstreamAnthropicAuth("api_key", byok)
    if not settings.allow_client_provider_key:
        return None
    # x-api-key is reserved for Anthropic/Claude clients.
    api_key = request.headers.get("x-api-key")
    if api_key:
        return UpstreamAnthropicAuth("api_key", api_key)
    # Bearer is a provider credential only when it is not a Decompute dc_* platform key. Local Claude
    # subscription users need this passthrough; hosted production gateways block it by default.
    bearer = extract_bearer(request.headers.get("authorization"))
    if bearer and not bearer.startswith("dc_"):
        if settings.allow_client_bearer_provider_auth:
            return UpstreamAnthropicAuth("bearer", bearer)
    return None


def upstream_anthropic_key(request: Request, settings: Settings) -> str | None:
    """Legacy helper for API-key-only call sites."""
    auth = upstream_anthropic_auth(request, settings)
    if auth and auth.kind == "api_key":
        return auth.value
    return None


def assert_csrf_for_browser_mutation(request: Request) -> None:
    # API key clients are not browsers. For browser-origin admin mutations, require a CSRF header.
    origin = request.headers.get("origin")
    method = request.method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and origin and not request.headers.get("x-decompute-csrf"):
        raise HTTPException(status_code=403, detail="Missing x-decompute-csrf header")
