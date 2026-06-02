from __future__ import annotations

"""FastAPI admin router for creating/downloading support bundles.

Integration (see main.py):

    from .support_router import include_support_bundle_router

    include_support_bundle_router(
        app, store=store, settings=settings,
        auth_context_func=lambda request: _support_auth_ctx(request),
        capabilities_func=lambda: capabilities_dict(settings),
        health_func=health,
    )

The endpoint requires admin/support scope. `is_support_admin` accepts a context with
auth_mode=dev, an is_admin marker, or scopes containing support:read / support:write /
admin — which is how this branch's AuthPrincipal is adapted into a dict.
"""

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .support_bundle import BundleOptions, SupportBundleBuilder


class SupportBundleRequest(BaseModel):
    # passthrough is intentionally NOT accepted over the API: config policy is that only
    # tokenize/redact are release-safe, and a caller must not be able to opt out of PII scrubbing.
    pii_mode: str = Field(default="tokenize", pattern="^(tokenize|redact)$")
    include_logs: bool = True
    include_reports: bool = True
    include_env: bool = True
    max_log_bytes: int = Field(default=250_000, ge=10_000, le=2_000_000)
    release_version: str = "v0.1.0-beta.1"
    download: bool = True


def is_support_admin(ctx: dict[str, Any]) -> bool:
    if ctx.get("auth_mode") == "dev":
        return True
    if ctx.get("is_admin") is True:
        return True
    scopes = set(ctx.get("scopes") or [])
    return bool({"support:read", "support:write", "admin"} & scopes)


def include_support_bundle_router(
    app,
    *,
    store: Any,
    settings: Any,
    auth_context_func: Callable[[Request], dict[str, Any]],
    capabilities_func: Callable[[], Any] | None = None,
    health_func: Callable[[], Any] | None = None,
    route_prefix: str = "/v1/support",
) -> None:
    router = APIRouter(prefix=route_prefix, tags=["support"])

    @router.post("/bundle")
    async def create_support_bundle(payload: SupportBundleRequest, request: Request):
        if not bool(getattr(settings, "support_bundle_enabled", True)):
            raise HTTPException(status_code=404, detail="support bundle disabled")
        ctx = auth_context_func(request)
        if not is_support_admin(ctx):
            raise HTTPException(status_code=403, detail="support bundle requires admin/support scope")

        data_dir = Path(getattr(settings, "data_dir", ".decompute"))
        output_dir = Path(getattr(settings, "support_bundle_output_dir", data_dir / "support"))
        # Server settings are a policy CEILING: a request may only narrow what is
        # collected / how strongly it is scrubbed, never widen it past operator intent.
        settings_mode = getattr(settings, "support_bundle_pii_mode", "tokenize")
        pii_mode = "redact" if settings_mode == "redact" else payload.pii_mode
        options = BundleOptions(
            output_dir=output_dir,
            data_dir=data_dir,
            project_root=Path.cwd(),
            release_version=payload.release_version,
            pii_mode=pii_mode,
            include_logs=payload.include_logs and bool(getattr(settings, "support_bundle_include_logs", True)),
            include_reports=payload.include_reports and bool(getattr(settings, "support_bundle_include_reports", True)),
            include_env=payload.include_env and bool(getattr(settings, "support_bundle_include_env", True)),
            max_log_bytes=min(payload.max_log_bytes, int(getattr(settings, "support_bundle_max_log_bytes", payload.max_log_bytes))),
        )

        def default_health() -> dict[str, Any]:
            return {
                "service": getattr(settings, "app_name", "Decompute Gateway"),
                "environment": getattr(settings, "environment", "unknown"),
                "mock_anthropic": bool(getattr(settings, "mock_anthropic", False)),
                "prompt_cache_mode": getattr(settings, "prompt_cache_mode", None),
            }

        def health_with_requester() -> dict[str, Any]:
            snap = dict(health_func() if health_func else default_health())
            # Annotate who requested the bundle (metadata only — no secrets/PII).
            snap["support_bundle_requested_by"] = {
                "project_id": ctx.get("project_id"),
                "key_prefix": ctx.get("key_prefix"),
                "auth_mode": ctx.get("auth_mode"),
            }
            return snap

        result = SupportBundleBuilder(
            options=options,
            store=store,
            settings=settings,
            health_snapshot_func=health_with_requester,
            capabilities_func=capabilities_func,
        ).build()

        # Audit hook — never fail bundle creation if auditing fails.
        if hasattr(store, "record_audit"):
            try:
                store.record_audit(
                    project_id=ctx.get("project_id"),
                    actor_key_prefix=ctx.get("key_prefix"),
                    action="support_bundle.create",
                    target_type="support_bundle",
                    target_id=result.path.name,
                    request_id=request.headers.get("x-decompute-request-id"),
                    metadata={"path": str(result.path), "leak_scan_ok": result.leak_scan.get("ok")},
                )
            except Exception:
                pass

        if not result.leak_scan.get("ok"):
            return JSONResponse(
                status_code=500,
                content={
                    "error": "support_bundle_leak_scan_failed",
                    "message": "Support bundle was created but failed the local leak scan; do not share it.",
                    "result": result.to_dict(),
                },
            )

        if payload.download:
            return FileResponse(
                path=result.path,
                filename=result.path.name,
                media_type="application/zip",
                headers={
                    "x-decompute-support-bundle": result.path.name,
                    "x-decompute-support-leak-scan-ok": "true",
                },
            )
        return result.to_dict()

    app.include_router(router)
