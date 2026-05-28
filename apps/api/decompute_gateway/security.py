from __future__ import annotations

from fastapi import Header, HTTPException, Request
from .config import Settings
from .db import Store


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization


def platform_api_key_from_request(request: Request) -> str | None:
    # Prefer dedicated Decompute header. Fall back to Bearer only for non-Claude platform endpoints.
    return (
        request.headers.get("x-decompute-key")
        or request.headers.get("x-decompute-api-key")
        or request.headers.get("x-api-key")
        or extract_bearer(request.headers.get("authorization"))
    )


def auth_context(request: Request, store: Store, settings: Settings) -> dict:
    raw = platform_api_key_from_request(request)

    if raw == settings.dev_api_key:
        return {"project_id": "proj_default", "key_prefix": raw[:10], "auth_mode": "dev"}

    found = store.verify_api_key(raw or "")
    if found:
        return {"project_id": found["project_id"], "key_prefix": found["key_prefix"], "auth_mode": "api_key"}

    if settings.require_auth:
        raise HTTPException(status_code=401, detail="Missing or invalid Decompute API key")

    return {"project_id": "proj_default", "key_prefix": None, "auth_mode": "local_noauth"}


def upstream_anthropic_key(request: Request, settings: Settings) -> str | None:
    if settings.anthropic_api_key:
        return settings.anthropic_api_key
    if not settings.allow_client_provider_key:
        return None
    # Claude/Anthropic-style clients usually send x-api-key. Some gateways use Bearer.
    return request.headers.get("x-api-key") or extract_bearer(request.headers.get("authorization"))
