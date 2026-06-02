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

import datetime as _dt
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .support_bundle import BundleOptions, SupportBundleBuilder, scan_bundle_for_leaks

_REL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_timestamp(value: str | None) -> _dt.datetime | None:
    """Parse a since/until value: ISO 8601, a unix epoch, or a relative span (e.g. 2h, 7d)."""
    if not value:
        return None
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    rel = _REL_RE.match(value)
    if rel:
        return now - _dt.timedelta(seconds=int(rel.group(1)) * _REL_UNITS[rel.group(2).lower()])
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Normalize naive (date-only / tz-less) input to UTC — comparing a naive value
        # against a timestamptz column raises on Postgres (a 500). Mirrors main._parse_iso.
        return dt.replace(tzinfo=_dt.timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        pass
    try:
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"unparseable timestamp: {value!r} (use ISO 8601, unix epoch, or '2h'/'7d')")


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
    user_auth_func: Callable[[Request], dict[str, Any]] | None = None,
    route_prefix: str = "/v1/support",
) -> None:
    # admin/support scope gates creating + reading reports; uploading a bundle is a normal
    # USER action (they send their own), so it uses user_auth_func (any authenticated key).
    user_auth = user_auth_func or auth_context_func
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

    @router.get("/session")
    async def support_session_report(request: Request, session_id: str, since: str | None = None, until: str | None = None):
        """Privacy-safe per-session diagnostics (metadata only) for a support bundle.

        Returns request IDs, endpoints, models, statuses, token/latency summaries — never
        prompts or response bodies. The CLI folds this into a `decompute support bundle`.
        """
        ctx = auth_context_func(request)
        if not is_support_admin(ctx):
            raise HTTPException(status_code=403, detail="support session report requires admin/support scope")
        if not hasattr(store, "support_session_report"):
            raise HTTPException(status_code=501, detail="store does not support session reports")
        return store.support_session_report(
            project_id=ctx.get("project_id"),
            session_id=session_id,
            since=parse_timestamp(since),
            until=parse_timestamp(until),
        )

    @router.post("/upload")
    async def upload_support_bundle(request: Request):
        """Receive a privacy-safe support bundle from a user (`decompute support send`).

        Authenticated as the uploading user (their own bundle, their own project). The zip is
        re-scanned for leaks server-side — a bundle that fails is rejected, never stored — then
        filed in the per-project support inbox and a reference id is returned for the user to quote.
        """
        ctx = user_auth(request)
        project_id = ctx.get("project_id") or "unknown"
        data = await request.body()
        if not data or data[:2] != b"PK":
            raise HTTPException(status_code=400, detail="expected a zip body (Content-Type: application/zip)")

        # Defense in depth: re-run the leak scan on what actually arrived; never store a leaker.
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            scan = scan_bundle_for_leaks(Path(tmp.name))
        if not scan.get("ok"):
            raise HTTPException(status_code=422, detail={"error": "leak_scan_failed", "suspicious": scan.get("suspicious")})

        reference_id = "sup_" + secrets.token_hex(6)
        data_dir = Path(getattr(settings, "data_dir", ".decompute"))
        inbox = data_dir / "support_inbox" / project_id
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{reference_id}.zip").write_bytes(data)

        if hasattr(store, "record_audit"):
            try:
                store.record_audit(
                    project_id=ctx.get("project_id"),
                    actor_key_prefix=ctx.get("key_prefix"),
                    action="support_bundle.upload",
                    target_type="support_bundle",
                    target_id=reference_id,
                    request_id=request.headers.get("x-decompute-request-id"),
                    metadata={"bytes": len(data), "leak_scan_ok": True},
                )
            except Exception:
                pass

        return {"reference_id": reference_id, "stored": True, "bytes": len(data), "leak_scan_ok": True}

    app.include_router(router)
