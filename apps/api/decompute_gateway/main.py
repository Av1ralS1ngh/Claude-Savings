from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import csv
import html
import io
import json
import logging
import secrets
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from .anthropic_provider import ProviderResult, call_anthropic_messages, count_tokens, list_anthropic_models, stream_anthropic_messages, _request_has_cache_control
from .capabilities import capabilities_dict
from .config import get_settings
from .crypto import CCRCrypto
from .db import ROLLING_MEDIAN_WINDOW, Store
from .licensing import DEVICE_HEADER, LicenseService, device_fingerprint_from_request
from .observability import BodySizeLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware, configure_logging, current_request_id, metrics_response, project_id_var
from .pricing import cost_usd, savings_usd
from .billing import BillingError, BillingService
from .rate_limits import RateLimitManager
from .secret_scanner import SecretScanner
from .security import AuthPrincipal, assert_csrf_for_browser_mutation, auth_context, upstream_anthropic_key
from .sse import SSEUsageTracker
from .streaming_cleanup import StreamUsageTracker, proxy_sse_stream
from .support_router import include_support_bundle_router
from .workload import classify_workload
from .yscompress import CCR_RETRIEVE_TOOL, YSCompressClient, estimate_tokens
from .yellowstone import TrafficProfile, YellowstonePlanner, scan_local_devices

settings = get_settings()
configure_logging()
log = logging.getLogger("decompute.gateway")
store = Store(settings)
if settings.uses_sqlite:
    # SQLite has no Alembic step — build the schema from the ORM, then seed.
    store.create_schema_for_local_dev()
else:
    # Postgres schema comes from `alembic upgrade head`, which creates tables
    # but does not seed rows. The default org/project must still exist or every
    # local_noauth/dev request (project_id=proj_default) fails the FK constraint.
    store.ensure_default_project()
crypto = CCRCrypto(settings.ccr_encryption_key, settings.ccr_key_id)
scanner = SecretScanner(mode=settings.pii_mode)
yscompress = YSCompressClient(settings, store, crypto, scanner)
rate_limits = RateLimitManager(settings, store=store)  # store-backed: per-project quota follows the licensed tier
billing = BillingService(settings, store)
licensing = LicenseService(settings, store)  # device axis: per-device Pro license + free compression quota

app = FastAPI(
    title="Decompute Claude Optimization Gateway",
    version="0.2.0-v0-p0",
    description="YSCompress + Yellowstone gateway for Claude token/compute optimization.",
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(RequestContextMiddleware)
if settings.security_headers:
    app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "x-api-key", "x-decompute-api-key", "x-decompute-key", "x-decompute-csrf", "x-decompute-request-id", "x-decompute-session-id", "x-decompute-device", "anthropic-beta", "x-yscompress-bypass", "x-headroom-bypass"],
    expose_headers=["x-decompute-request-id", "x-decompute-tokens-before", "x-decompute-tokens-after", "x-decompute-route", "request-id", "x-decompute-tier", "x-decompute-license-status", "x-decompute-quota-remaining"],
)


class ProjectCreate(BaseModel):
    name: str
    org_id: str = "org_default"
    allowed_models: list[str] | None = None
    ccr_enabled: bool | None = None


class ApiKeyCreate(BaseModel):
    name: str = "default"
    project_id: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["messages:write", "usage:read"])

    @field_validator("scopes")
    @classmethod
    def scopes_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("scopes must not be empty; provide at least one scope")
        return v


class MessageContent(BaseModel):
    role: str
    # content is either a plain string or a list of Anthropic content blocks
    # (text, tool_use, tool_result, image, ...).
    content: str | list[dict[str, Any]]


class MessagesRequest(BaseModel):
    model: str = "claude-haiku-4-5"
    max_tokens: int = 200
    messages: list[MessageContent] = Field(
        default_factory=lambda: [
            MessageContent(role="user", content="Search codebase for compress_messages."),
            MessageContent(role="assistant", content="Found 47 refs:\nsrc/compression/mod.rs:12 - pub fn compress_messages\nsrc/gateway/proxy.rs:45 - compress_messages(&body.messages)\nsrc/gateway/stream.rs:23 - compress_messages(&chunk)\nsrc/tests/integration.rs:56 - compress_messages(&large_msgs)"),
            MessageContent(role="user", content="Run the build.\n\n2026-05-30T00:00:01Z INFO compiling step 1 ... ok\n2026-05-30T00:00:02Z INFO compiling step 2 ... ok\n2026-05-30T00:00:03Z INFO compiling step 3 ... ok\n2026-05-30T00:00:04Z INFO compiling step 4 ... ok\n2026-05-30T00:00:05Z INFO compiling step 5 ... ok\n2026-05-30T00:00:06Z ERROR cannot find symbol `compress_messages` for architecture arm64\n2026-05-30T00:00:06Z FATAL build failed after 3 retries"),
        ]
    )
    # Anthropic's Messages API allows `system` to be a plain string OR an array of
    # content blocks (text blocks, optionally with cache_control). Claude Code sends the
    # array form for prompt caching, so accept both — mirrors MessageContent.content above.
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    decompute: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


class CompressionPreview(BaseModel):
    messages: list[dict[str, Any]]
    mode: str = "safe"
    ccr: bool | None = None


class RoutePlanRequest(BaseModel):
    model: str = "claude-sonnet-4-5"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    raw_input_tokens: int | None = None
    yscompress_input_tokens: int | None = None
    yellowstone_final_input_tokens: int | None = None
    p95_output_tokens: int = 1024
    yscompress: dict[str, Any] = Field(default_factory=dict)
    prompt_cache: str = "auto"
    workload_class: str = "interactive"


class DeploymentCreate(BaseModel):
    name: str
    plan: dict[str, Any]


class ProjectLimitsUpdate(BaseModel):
    """Admin override of per-project quotas. Any field left null is unchanged;
    send an explicit null via the clear endpoint to reset to the global default."""
    per_key_rpm: float | None = None
    project_tokens_per_min: float | None = None
    max_context_tokens: float | None = None
    monthly_spend_usd: float | None = None


def _auth(request: Request, scope: str | None = None) -> AuthPrincipal:
    principal = auth_context(request, store, settings, required_scope=scope)
    project_id_var.set(principal.project_id)
    return principal


def _response_headers(*, request_id: str, comp: Any | None = None, plan: Any | None = None, upstream_request_id: str | None = None) -> dict[str, str]:
    headers = {"x-decompute-request-id": request_id}
    if upstream_request_id:
        headers["request-id"] = upstream_request_id
        headers["x-decompute-upstream-request-id"] = upstream_request_id
    if comp:
        headers.update({
            "x-decompute-tokens-before": str(comp.tokens_before),
            "x-decompute-tokens-after": str(comp.tokens_after),
            "x-decompute-tokens-saved": str(comp.tokens_saved),
            "x-decompute-compression-ratio": str(comp.compression_ratio),
        })
    if plan:
        headers["x-decompute-route"] = plan.route
        headers["x-decompute-yellowstone-final-tokens"] = str(plan.yellowstone_final_input_tokens)
    return headers


def _apply_license_headers(headers: dict[str, str], license_meta: dict[str, Any] | None) -> None:
    """Stamp the device-license decision onto the response. Invisible to Claude Code, but read by
    `decompute status`/`license` and the desktop upgrade modal (full x-decompute-* contract)."""
    if not license_meta:
        return
    # Legacy convenience headers (our CLI) + the full license contract (desktop modal).
    headers["x-decompute-tier"] = str(license_meta.get("tier", "free"))
    if license_meta.get("quota_remaining") is not None:
        headers["x-decompute-quota-remaining"] = str(license_meta.get("quota_remaining"))
    headers.update(LicenseService.decision_headers(license_meta))


async def _resolve_ccr_retrievals(result: ProviderResult, compressed_body: dict[str, Any], *, principal: AuthPrincipal, upstream_key: str | None, prompt_cache_mode: str, request: Request, max_rounds: int = 3) -> tuple[ProviderResult, int, int]:
    """If Claude calls ccr_retrieve, serve the stored original back and continue the
    conversation, so an elided reference never costs answer quality. Non-CCR tool
    calls are left untouched for the client.

    Returns (final_result, rounds_used, retrieved_tokens). retrieved_tokens is the
    size of the originals served back — these were 'un-saved' by retrieval, so the
    caller must add them to the recorded final-input count. CCR only nets savings
    when the summary suffices and Claude does NOT retrieve."""
    rounds = 0
    retrieved_tokens = 0
    convo = list(compressed_body.get("messages") or [])
    while rounds < max_rounds and isinstance(result.body, dict) and result.body.get("stop_reason") == "tool_use":
        content = result.body.get("content", []) or []
        ccr_calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "ccr_retrieve"]
        if not ccr_calls:
            break  # a real customer tool call — hand it back to the client untouched
        convo.append({"role": "assistant", "content": content})
        tool_results = []
        for tu in ccr_calls:
            cid = (tu.get("input") or {}).get("id", "")
            item = store.get_ccr(project_id=principal.project_id, ccr_id=cid, crypto=crypto, include_payload=True)
            original = (item or {}).get("original_text") or f"[ccr: content for {cid} not found]"
            retrieved_tokens += estimate_tokens(original)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.get("id"), "content": original})
        convo.append({"role": "user", "content": tool_results})
        next_body = dict(compressed_body)
        next_body["messages"] = convo
        result = await call_anthropic_messages(next_body, settings=settings, upstream_api_key=upstream_key, prompt_cache_mode=prompt_cache_mode, extra_headers=dict(request.headers))
        rounds += 1
    return result, rounds, retrieved_tokens


def _usage_from_result(result: ProviderResult) -> dict[str, int]:
    usage = result.body.get("usage") if isinstance(result.body, dict) else {}
    usage = usage or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
    }


async def _real_token_baseline(model: str | None, body: dict[str, Any], *, upstream_key: str | None) -> int | None:
    """Anthropic's real-tokenizer count of the UNCOMPRESSED prompt — the honest savings
    denominator. The compressor's len/4 estimate undercounts ~5x vs Anthropic's tokenizer,
    so comparing billed (real) compressed tokens against an estimated raw baseline mixes
    rulers and floors genuine savings to 0%. count_tokens puts both sides on the same ruler.

    Returns None on any failure (caller keeps the estimate). In mock mode count_tokens falls
    back to the same estimator, so this is a consistent no-op there. Caller runs it CONCURRENTLY
    with the generation call (it is far cheaper than generation) so it adds ~0 wall-time."""
    try:
        ct_body: dict[str, Any] = {"model": model, "messages": body.get("messages") or []}
        if body.get("system"):
            ct_body["system"] = body["system"]
        if body.get("tools"):
            ct_body["tools"] = body["tools"]
        res = await count_tokens(ct_body, settings=settings, upstream_api_key=upstream_key)
        if res.status_code < 400 and isinstance(res.body, dict):
            n = int(res.body.get("input_tokens") or 0)
            return n if n > 0 else None
    except Exception as exc:
        log.warning("count_tokens baseline failed; keeping estimate", extra={"extra": {"error": str(exc)}})
    return None


_SESSION_ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _clean_session_id(raw: str | None) -> str | None:
    """Sanitize the client-supplied x-decompute-session-id into a safe, bounded correlator.

    Keeps only [A-Za-z0-9._-] and clamps to 64 chars. This (a) keeps a PII-shaped value
    (email/hostname/free text) from being echoed back in a support report, and (b) prevents
    an over-128-char header from aborting the usage insert on Postgres (VARCHAR(128)).
    """
    if not raw:
        return None
    cleaned = "".join(c for c in raw if c in _SESSION_ID_ALLOWED)[:64]
    return cleaned or None


def _record_usage(*, principal: AuthPrincipal, endpoint: str, model: str | None, raw_input_tokens: int, yscompress_input_tokens: int, yellowstone_final_input_tokens: int, output_tokens: int, cache_creation_input_tokens: int, cache_read_input_tokens: int, prompt_cache_mode: str, plan: Any, request_id: str, upstream_request_id: str | None, status_code: int, start_time: float, workload_class: str | None = None, optimization_steps: list[dict[str, Any]] | None = None, stream_status: str | None = None, stream_cancelled: bool = False, stream_cancel_reason: str | None = None, stream_json: dict[str, Any] | None = None, session_id: str | None = None, billed_input_tokens: int | None = None, real_raw_input_tokens: int | None = None) -> None:
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    # Honest savings: measure BOTH sides on Anthropic's real tokenizer. real_raw_input_tokens
    # is count_tokens() of the uncompressed prompt; billed_input_tokens is what Anthropic actually
    # charged for the compressed request (cache-EXCLUSIVE — gross it back with the cache fields so
    # cost_usd's internal cache subtraction nets to the billed uncached figure). When either is
    # missing (mock, error, cancelled stream, CCR) we fall back to the compressor's len/4 estimate
    # so the row is never zeroed. raw stays the denominator; we do not replace one ruler with two.
    eff_raw = real_raw_input_tokens if real_raw_input_tokens is not None else raw_input_tokens
    eff_final = (billed_input_tokens + cache_creation_input_tokens + cache_read_input_tokens
                 if billed_input_tokens is not None else yellowstone_final_input_tokens)
    billable_equiv = eff_final
    cost = cost_usd(model=model, input_tokens=eff_final, output_tokens=output_tokens, cache_creation_input_tokens=cache_creation_input_tokens, cache_read_input_tokens=cache_read_input_tokens)
    saved = savings_usd(model=model, raw_input_tokens=eff_raw, final_input_tokens=eff_final, output_tokens=output_tokens, cache_creation_input_tokens=cache_creation_input_tokens, cache_read_input_tokens=cache_read_input_tokens)
    try:
        store.record_usage(
            {
                "endpoint": endpoint,
                "model": model,
                "request_id": request_id,
                "upstream_request_id": upstream_request_id,
                "session_id": session_id,
                "status_code": status_code,
                "latency_ms": elapsed_ms,
                "raw_input_tokens": eff_raw,
                "yscompress_input_tokens": yscompress_input_tokens,
                "yellowstone_final_input_tokens": eff_final,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "tokens_saved": max(0, eff_raw - eff_final),
                "billable_input_equivalent_tokens": billable_equiv,
                "estimated_cost_usd": cost,
                "estimated_savings_usd": saved,
                "compression_ratio": min(1.0, eff_final / max(eff_raw, 1)),
                "workload_class": workload_class,
                "cache_mode": prompt_cache_mode,
                "route": plan.to_dict() if plan else None,
                "optimization_steps": optimization_steps or [],
                "stream_status": stream_status,
                "stream_cancelled": stream_cancelled,
                "stream_cancel_reason": stream_cancel_reason,
                "stream_json": stream_json,
            },
            project_id=principal.project_id,
            key_prefix=principal.key_prefix,
        )
    except Exception as exc:
        # Usage recording must never crash a request that already has a response.
        # A DB outage after a successful Anthropic call would otherwise cause a 500
        # that hides the real response from the client.
        log.error("usage recording failed — response already sent, continuing",
                  extra={"extra": {"request_id": request_id, "error": str(exc)}})


@app.on_event("startup")
def startup() -> None:
    if settings.uses_sqlite:
        store.create_schema_for_local_dev()
    else:
        store.ensure_default_project()
    purged = store.purge_expired_ccr()
    if purged:
        log.info("purged expired CCR items", extra={"extra": {"purged": purged}})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": settings.app_name, "environment": settings.environment, "mock_anthropic": settings.mock_anthropic, "yscompress": bool(settings.yscompress_base_url), "prompt_cache_mode": settings.prompt_cache_mode, "capabilities": capabilities_dict(settings)}


@app.get("/livez")
def livez() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    checks: dict[str, Any] = {"db": False, "anthropic_configured": settings.mock_anthropic or bool(settings.anthropic_api_key) or settings.allow_client_provider_key, "yscompress_optional": True}
    try:
        with store.engine.connect() as conn:
            conn.execute(text("select 1"))
        checks["db"] = True
    except Exception as exc:
        checks["db_error"] = str(exc)
    ok = bool(checks["db"] and checks["anthropic_configured"])
    if not ok:
        raise HTTPException(status_code=503, detail=checks)
    return {"ok": True, "checks": checks}


@app.get("/metrics")
def metrics() -> Response:
    return metrics_response()


_DASHBOARD_TEMPLATE = (Path(__file__).parent / "dashboard_template.html").read_text(encoding="utf-8")
_WL_DOT_CLASSES = ["dot", "dot b", "dot o", "dot g"]
_REQ_PALETTE = ["var(--ink)", "var(--blue)", "var(--orange)", "var(--green)", "var(--dim)"]
_CAP_PILLS = {
    "rust_core": ["yscompress", "rust"],
    "kompress_ml": ["torch", "Kompress-base"],
    "ast_code_aware": ["tree-sitter", "multi-lang"],
    "ccr_active": ["encrypted", "reversible"],
}


def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return ""
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _row_saved_pct(r: dict[str, Any]) -> float:
    raw = r.get("raw_input_tokens") or 0
    sent = r.get("yellowstone_final_input_tokens") or 0
    return max(0.0, (1.0 - sent / raw) * 100.0) if raw else 0.0


def _dashboard_workload_rows(by_workload: list[dict[str, Any]]) -> str:
    if not by_workload:
        return '<tr><td colspan="5" style="text-align:center;color:var(--dim);padding:26px;font-family:var(--mono)">No requests yet.</td></tr>'
    rows = []
    for i, w in enumerate(by_workload):
        dot = _WL_DOT_CLASSES[i % len(_WL_DOT_CLASSES)]
        pct = float(w["savings_percent"])
        rows.append(
            f'<tr><td><div class="wl-name"><span class="{dot}"></span>{html.escape(str(w["workload_class"]))}</div></td>'
            f'<td class="r">{w["requests"]}</td>'
            f'<td class="r">{w["raw_input_tokens"]:,}</td>'
            f'<td class="r">{w["sent_input_tokens"]:,}</td>'
            f'<td><div class="wl-bar"><div class="wl-bar-track"><div class="wl-bar-fill" style="width:{min(100.0, max(0.0, pct)):.0f}%"></div></div>'
            f'<span class="wl-savings">{pct:.1f}%</span></div></td></tr>'
        )
    return "".join(rows)


def _dashboard_cap_cards(caps: dict[str, dict[str, Any]]) -> str:
    cards = []
    for name, c in caps.items():
        st = '<span class="st">active</span>' if c["active"] else '<span class="st off">inactive</span>'
        pills = _CAP_PILLS.get(name, []) if c["active"] else ["not configured"]
        pill_html = "".join(f'<span class="vp">{html.escape(p)}</span>' for p in pills)
        cards.append(
            f'<div class="cap-card"><div class="row"><span class="nm">{html.escape(name)}</span>{st}</div>'
            f'<div class="det">{html.escape(c["detail"])}</div>'
            f'<div class="ver">{pill_html}</div></div>'
        )
    return "".join(cards)


def _stream_status_badge(r: dict[str, Any]) -> str:
    """Inline-styled badge surfacing a cancelled / errored stream in the request feed."""
    if r.get("stream_cancelled"):
        return ('<span title="stream cancelled (client disconnect)" style="margin-left:6px;'
                'font-family:var(--mono);font-size:9.5px;color:#c04646;background:#fdecec;'
                'border-radius:4px;padding:1px 5px">cancelled</span>')
    st = r.get("stream_status")
    if st and st != "completed":
        return (f'<span title="stream {html.escape(str(st))}" style="margin-left:6px;'
                'font-family:var(--mono);font-size:9.5px;color:#9a6700;background:#fff4d6;'
                f'border-radius:4px;padding:1px 5px">{html.escape(str(st))}</span>')
    return ""


def _dashboard_request_rows(recent: list[dict[str, Any]]) -> str:
    if not recent:
        return '<div class="req-empty">No requests yet — run a prompt through /v1/messages.</div>'
    colors: dict[str, str] = {}
    rows = []
    for r in recent:
        wl = r.get("workload_class") or "unclassified"
        if wl not in colors:
            colors[wl] = _REQ_PALETTE[len(colors) % len(_REQ_PALETTE)]
        raw = r.get("raw_input_tokens") or 0
        sent = r.get("yellowstone_final_input_tokens") or 0
        rid = (r.get("id") or "")[-4:]
        cost = r.get("estimated_cost_usd") or 0.0
        rows.append(
            f'<div class="req-row"><span class="req-id">#{html.escape(rid)}</span>'
            f'<div class="req-wl"><span class="d" style="background:{colors[wl]}"></span>{html.escape(wl)}{_stream_status_badge(r)}</div>'
            f'<div class="req-tokens"><span class="a">{raw:,}</span><span class="arr">&#8594;</span><span class="b">{sent:,}</span></div>'
            f'<span class="req-saved">{_row_saved_pct(r):.1f}%</span>'
            f'<span class="req-cost">${cost:.4f}</span>'
            f'<span class="req-time">{_ago(r.get("created_at"))}</span></div>'
        )
    return "".join(rows)


def _dur_compact(seconds: float) -> str:
    """Compact elapsed-time label for chart axis ticks: now / 15m / 2h / 3d / 2w."""
    s = int(max(0.0, seconds))
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 604800:
        return f"{s // 86400}d"
    return f"{s // 604800}w"


# Max number of recent requests plotted as individual points on the performance chart.
_CHART_POINTS = 60
# x of the 5 axis ticks in the SVG (must match the <text> positions in the template),
# and the fractions across the plotted points [oldest .. newest] they label (rightmost = newest).
_CHART_TICKS = (40, 220, 400, 580, 730)
_CHART_TICK_FRACS = (0.0, 0.25, 0.5, 0.75, 1.0)


def _dashboard_chart(series: dict[str, Any], now: datetime) -> dict[str, Any]:
    """SVG path 'd' strings + end-dot coords + axis labels for the performance chart, built
    from the recent PER-REQUEST points (see Store.usage_savings_timeseries). One point per
    request, spaced evenly left->right with the newest at the right edge, so the chart's
    end point IS the latest request — it equals the headline "Latest request" % exactly and
    updates the moment a new request arrives. (Time-bucketing was replaced because a burst of
    requests in one turn averaged into a single diluted slice, so the right edge never matched
    the headline.) x-axis ticks are labelled with the elapsed time of the point at each tick
    position, so the timeline stays meaningful even though spacing is per-request. Stable on
    reload: identical points render identically. Same shape for initial render and JS swap."""
    empty = {"savedAreaD": "", "savedLineD": "", "sentAreaD": "", "sentLineD": "",
             "dotX": -10, "dotY": -10, "xLabels": ["", "", "", "", ""], "hasData": False}
    points = series.get("points") or []
    if not points:
        return empty
    n = len(points)
    x0, x1 = 40.0, 760.0

    def xf(i: int) -> float:
        # newest point (i == n-1) anchors the right edge; a lone point also sits at the right
        return x1 if n == 1 else x0 + (x1 - x0) * (i / (n - 1))

    def yf(pct: float) -> float:
        return 220.0 - (max(0.0, min(100.0, pct)) / 100.0) * 200.0

    saved_xy = [(xf(i), yf(p["saved_pct"])) for i, p in enumerate(points)]
    sent_xy = [(xf(i), yf(100.0 - p["saved_pct"])) for i, p in enumerate(points)]

    def line(xy):
        if len(xy) == 1:  # a single point: draw a flat segment so the line/area is visible
            (x, y) = xy[0]
            return f"M{x0:.1f},{y:.1f} L{x:.1f},{y:.1f}"
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)

    def area(xy):
        base = yf(0.0)  # fill down to the 0% baseline, not past it
        left = x0 if len(xy) == 1 else xy[0][0]
        return f'{line(xy)} L{xy[-1][0]:.1f},{base:.1f} L{left:.1f},{base:.1f} Z'

    # Label each tick with the elapsed time of the point nearest that position.
    x_labels = [
        _dur_compact((now - points[round(f * (n - 1))]["created_at"]).total_seconds())
        for f in _CHART_TICK_FRACS
    ]

    return {
        "savedAreaD": area(saved_xy),
        "savedLineD": line(saved_xy),
        "sentAreaD": area(sent_xy),
        "sentLineD": line(sent_xy),
        "dotX": round(saved_xy[-1][0], 1),
        "dotY": round(saved_xy[-1][1], 1),
        "xLabels": x_labels,
        "hasData": True,
    }


def _dashboard_benchmark() -> tuple[str, str]:
    """Latest versioned benchmark report -> (meta, table-rows HTML) for the dashboard
    benchmark tab. Neutral terminology; reads benchmarks/reports/index.json -> latest."""
    reports = Path(__file__).resolve().parents[3] / "benchmarks" / "reports"
    try:
        idx = json.loads((reports / "index.json").read_text(encoding="utf-8"))
        rep = json.loads((reports / f"{idx['latest']}.json").read_text(encoding="utf-8"))
    except Exception:
        return "no report yet", ('<tr><td colspan="5" style="color:var(--dim);font-family:var(--sans)">'
                                 'No benchmark report yet — run <code>scripts/rc_benchmark.py --release &lt;version&gt;</code>.</td></tr>')
    meta = f"{rep.get('release', '?')} · gate {'PASS' if rep.get('gate_passed') else 'FAIL'}"
    rows = []
    for r in rep.get("comparison", []):
        delta = r.get("delta_pct", 0) or 0
        dstr = f"+{delta:g}" if delta >= 0 else f"{delta:g}"
        dcolor = "var(--green-deep)" if delta >= 0 else "#c04646"
        status = r.get("status", "")
        scolor = "var(--green-deep)" if status.startswith("Pass") else "var(--orange)"
        rows.append(
            f'<tr><td>{html.escape(str(r.get("workload", "")))}</td>'
            f'<td class="r">{r.get("reference_baseline_pct", "")}%</td>'
            f'<td class="r">{r.get("decompute_pct", ""):g}%</td>'
            f'<td class="r" style="color:{dcolor}">{dstr}</td>'
            f'<td class="r" style="color:{scolor};font-weight:600">{html.escape(status)}</td></tr>'
        )
    return html.escape(meta), "".join(rows)


_RANGE_LABELS = {
    "1h": ("last 1h", "last hour"),
    "24h": ("last 24h", "last 24 hours"),
    "7d": ("last 7d", "last 7 days"),
    "30d": ("last 30d", "last 30 days"),
    "all": ("all time", "all time"),
}


def _range_payload(
    usage: dict[str, Any],
    recent: list[dict[str, Any]],
    lifetime: dict[str, Any],
    chart: dict[str, Any],
    name: str = "24h",
) -> dict[str, Any]:
    """All display strings + HTML fragments for one time range. Used both to render
    the default range server-side and (as JSON) to swap ranges client-side.

    Range-scoped (changes with the tab): latest request, rolling median, workloads,
    request feed, and the performance `chart`. Constant across tabs: the two Lifetime
    cards ($ saved / cost), which always read from `lifetime` (all-time) — switching
    the range must not move a "Lifetime" number."""
    range_label, range_label_long = _RANGE_LABELS.get(name, ("last 24h", "last 24 hours"))
    latest = usage["latest_savings_percent"]
    median = usage["median_savings_percent"]
    reqs = usage["requests"]
    d = latest - median
    if not reqs:
        d_latest = "no requests yet"
    else:
        d_latest = (f"&#8593; {d:.1f} pts vs median" if d >= 0 else f"&#8595; {abs(d):.1f} pts vs median")
    plural = "s" if reqs != 1 else ""
    # The headline median is a ROLLING median over the latest ROLLING_MEDIAN_WINDOW (50)
    # requests, so its subtitle counter must reflect that window — it climbs 1..50 as
    # history accumulates and then holds at 50, never the (unbounded) lifetime total.
    median_n = min(reqs, ROLLING_MEDIAN_WINDOW)
    median_plural = "s" if median_n != 1 else ""
    # Lifetime cards: always all-time, never range-scoped.
    life_reqs = lifetime["requests"]
    life_plural = "s" if life_reqs != 1 else ""
    return {
        "latest": f"{latest:.1f}%",
        "median": f"{median:.1f}%",
        "saved": f"${lifetime['estimated_savings_usd']:.4f}",
        "cost": f"${lifetime['estimated_cost_usd']:.4f}",
        "dLatest": d_latest,
        "dMedian": f"rolling &#183; latest {median_n} request{median_plural}",
        "dSaved": f"{lifetime['tokens_saved']:,} tokens compressed",
        "dCost": f"{life_reqs} request{life_plural}",
        "wlMeta": f"{reqs} request{plural}",
        "reqMeta": f"live &#183; last {len(recent)}",
        "rangeLabel": range_label,
        "rangeLabelLong": range_label_long,
        "workloadRows": _dashboard_workload_rows(usage.get("by_workload", [])),
        "requestRows": _dashboard_request_rows(recent),
        "chart": chart,
    }


_DASHBOARD_RANGES = {"1h": 1.0 / 24, "24h": 1.0, "7d": 7.0, "30d": 30.0, "all": None}
_DASHBOARD_DEFAULT_RANGE = "1h"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, response: Response) -> str:
    # Never let the browser serve a stale dashboard from cache — always fetch fresh,
    # so template/JS changes (e.g. the time-range tabs) show up on a normal reload.
    response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    # The global CSP (SecurityHeadersMiddleware) blocks inline <script>, which would kill
    # the dashboard's tab/chart JS. Allow THIS page's script via a per-request nonce —
    # no 'unsafe-inline', so the API surface keeps its strict policy. setdefault in the
    # middleware means this route-level CSP wins.
    nonce = secrets.token_urlsafe(16)
    response.headers["content-security-policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
    )
    principal = _auth(request, "dashboard:read") if settings.dashboard_auth_required else _auth(request)
    caps = capabilities_dict(settings)
    project = store.get_project(principal.project_id) or {}
    pname = project.get("name") or "Default Project"
    initials = ("".join(w[0] for w in pname.split()[:2]) or "DP").upper()
    # Honest count: number of API keys on the project (no fabricated floor).
    api_key_count = len(store.list_api_keys(project_id=principal.project_id))

    # Real health for the status pill — reuse the /readyz logic. We reached this route
    # only because the DB answered the usage queries, so DB is up; the upstream/Anthropic
    # config is the part that can be missing while the page still renders.
    upstream_ok = settings.mock_anthropic or bool(settings.anthropic_api_key) or settings.allow_client_provider_key
    status_text = "all systems operational" if upstream_ok else "upstream not configured"
    status_class = "" if upstream_ok else "degraded"

    now = datetime.now(timezone.utc)
    # All-time summary feeds the two Lifetime cards, which stay constant across tabs.
    lifetime = store.usage_summary(project_id=principal.project_id, since=None)
    payloads: dict[str, dict[str, Any]] = {}
    for name, days in _DASHBOARD_RANGES.items():
        since = None if days is None else now - timedelta(days=days)
        u = store.usage_summary(project_id=principal.project_id, since=since)
        rec = store.recent_usage(project_id=principal.project_id, limit=6, since=since)
        # Performance chart: the most recent requests in this range's window, one point
        # each (newest anchors the right edge), so the end point == the headline latest %.
        series = store.usage_savings_timeseries(
            project_id=principal.project_id, since=since, until=now, limit=_CHART_POINTS)
        chart = _dashboard_chart(series, now)
        payloads[name] = _range_payload(u, rec, lifetime, chart, name)

    dp = payloads[_DASHBOARD_DEFAULT_RANGE]
    chart = dp["chart"]
    bench_meta, bench_rows = _dashboard_benchmark()
    # Embed all-range data for client-side tab switching; protect the </script> close.
    data_json = json.dumps(payloads).replace("</", "<\\/")
    repl = {
        "__PROJECT_INITIALS__": html.escape(initials),
        "__PROJECT_NAME__": html.escape(pname),
        "__MEMBERS__": str(api_key_count),
        "__STATUS_TEXT__": html.escape(status_text),
        "__STATUS_CLASS__": status_class,
        "__BENCHMARK_META__": bench_meta,
        "__BENCHMARK_ROWS__": bench_rows,
        "__USER_INITIALS__": "LS",
        "__USER_NAME__": "Local session",
        "__USER_EMAIL__": html.escape(principal.key_prefix or settings.dev_api_key),
        "__LATEST__": dp["latest"],
        "__LATEST_DELTA__": dp["dLatest"],
        "__MEDIAN__": dp["median"],
        "__MEDIAN_DELTA__": dp["dMedian"],
        "__SAVED_USD__": dp["saved"],
        "__SAVED_DELTA__": dp["dSaved"],
        "__COST_USD__": dp["cost"],
        "__COST_DELTA__": dp["dCost"],
        "__WL_META__": dp["wlMeta"],
        "__RANGE_LABEL__": dp["rangeLabel"],
        "__RANGE_LABEL_LONG__": dp["rangeLabelLong"],
        "__WORKLOAD_ROWS__": dp["workloadRows"],
        "__CAP_CARDS__": _dashboard_cap_cards(caps),
        "__REQ_META__": dp["reqMeta"],
        "__REQUEST_ROWS__": dp["requestRows"],
        "__CH_SAVED_AREA_D__": chart["savedAreaD"],
        "__CH_SAVED_LINE_D__": chart["savedLineD"],
        "__CH_SENT_AREA_D__": chart["sentAreaD"],
        "__CH_SENT_LINE_D__": chart["sentLineD"],
        "__CH_DOT_X__": str(chart["dotX"]),
        "__CH_DOT_Y__": str(chart["dotY"]),
        "__CH_AXIS_0__": chart["xLabels"][0],
        "__CH_AXIS_1__": chart["xLabels"][1],
        "__CH_AXIS_2__": chart["xLabels"][2],
        "__CH_AXIS_3__": chart["xLabels"][3],
        "__CH_AXIS_4__": chart["xLabels"][4],
        "__RANGE_DATA_JSON__": data_json,
        "__SCRIPT_NONCE__": nonce,
    }
    out = _DASHBOARD_TEMPLATE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>None yet.</p>"
    normalized = []
    for r in rows:
        out = {}
        for k, v in r.items():
            out[k] = html.escape(json.dumps(v)[:180] if isinstance(v, (dict, list)) else str(v))
        normalized.append(out)
    cols = list(normalized[0].keys())[:10]
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{row.get(c,'')}</td>" for c in cols) + "</tr>" for row in normalized)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@app.post("/v1/projects")
def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
    principal = _auth(request, "admin")
    assert_csrf_for_browser_mutation(request)
    project = store.create_project(payload.name, org_id=payload.org_id, allowed_models=payload.allowed_models, ccr_enabled=payload.ccr_enabled)
    store.record_audit(project_id=project["id"], actor_key_prefix=principal.key_prefix, action="project.create", target_type="project", target_id=project["id"], request_id=current_request_id())
    return project


@app.get("/v1/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    principal = _auth(request, "admin")
    return store.list_projects(org_id=principal.org_id)


@app.get("/v1/projects/{project_id}/limits")
def get_project_limits(project_id: str, request: Request) -> dict[str, Any]:
    _auth(request, "admin")
    return {"project_id": project_id, "effective": rate_limits.effective_limits(project_id), "overrides": rate_limits.overrides.get(project_id, {})}


@app.put("/v1/projects/{project_id}/limits")
def set_project_limits(project_id: str, payload: ProjectLimitsUpdate, request: Request) -> dict[str, Any]:
    """Temporarily raise (or lower) a project's quota. Admin-only; process-local for V0."""
    principal = _auth(request, "admin")
    assert_csrf_for_browser_mutation(request)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No limit fields provided")
    effective = rate_limits.set_override(project_id, **updates)
    store.record_audit(project_id=project_id, actor_key_prefix=principal.key_prefix, action="project.limits.set", target_type="project", target_id=project_id, request_id=current_request_id(), metadata=updates)
    return {"project_id": project_id, "effective": effective, "overrides": rate_limits.overrides.get(project_id, {})}


@app.delete("/v1/projects/{project_id}/limits")
def clear_project_limits(project_id: str, request: Request) -> dict[str, Any]:
    principal = _auth(request, "admin")
    assert_csrf_for_browser_mutation(request)
    rate_limits.clear_override(project_id)
    store.record_audit(project_id=project_id, actor_key_prefix=principal.key_prefix, action="project.limits.clear", target_type="project", target_id=project_id, request_id=current_request_id())
    return {"project_id": project_id, "effective": rate_limits.effective_limits(project_id), "overrides": {}}


@app.post("/v1/api_keys")
def create_api_key(payload: ApiKeyCreate, request: Request) -> dict[str, Any]:
    principal = _auth(request, "keys:admin")
    assert_csrf_for_browser_mutation(request)
    project_id = payload.project_id or principal.project_id
    if project_id != principal.project_id and "admin" not in principal.scopes:
        raise HTTPException(status_code=403, detail="Cannot create keys for another project")
    return store.create_api_key(project_id=project_id, name=payload.name, scopes=payload.scopes)


@app.get("/v1/api_keys")
def list_api_keys(request: Request) -> list[dict[str, Any]]:
    principal = _auth(request, "keys:admin")
    return store.list_api_keys(project_id=principal.project_id)


@app.post("/v1/api_keys/{key_id}/revoke")
def revoke_api_key(key_id: str, request: Request) -> dict[str, Any]:
    principal = _auth(request, "keys:admin")
    assert_csrf_for_browser_mutation(request)
    ok = store.revoke_api_key(project_id=principal.project_id, key_id=key_id, actor_prefix=principal.key_prefix, request_id=current_request_id())
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "revoked": key_id}


@app.post("/v1/devices/scan")
def scan_devices(request: Request) -> list[dict[str, Any]]:
    principal = _auth(request, "devices:write")
    profiles = [d.to_dict() for d in scan_local_devices()]
    for p in profiles:
        store.upsert_device(p, project_id=principal.project_id)
    return profiles


@app.get("/v1/devices")
def list_devices(request: Request) -> list[dict[str, Any]]:
    principal = _auth(request, "devices:read")
    return store.list_devices(project_id=principal.project_id)


@app.post("/v1/compression/preview")
async def compression_preview(payload: CompressionPreview, request: Request) -> dict[str, Any]:
    principal = _auth(request, "messages:write")
    result = await yscompress.compress_messages(payload.messages, project_id=principal.project_id, mode=payload.mode, ccr=payload.ccr)
    return result.to_dict()


@app.post("/v1/routes/plan")
async def route_plan(payload: RoutePlanRequest, request: Request) -> dict[str, Any]:
    principal = _auth(request, "routes:read")
    if payload.raw_input_tokens is not None and payload.yscompress_input_tokens is not None:
        raw_tokens = payload.raw_input_tokens
        ys_tokens = payload.yscompress_input_tokens
        final_tokens = payload.yellowstone_final_input_tokens or ys_tokens
    else:
        comp = await yscompress.compress_messages(payload.messages, project_id=principal.project_id, mode=payload.yscompress.get("mode", settings.yscompress_default_mode), ccr=payload.yscompress.get("ccr", settings.ccr_enabled))
        raw_tokens, ys_tokens, final_tokens = comp.tokens_before, comp.tokens_after, comp.tokens_after
    planner = YellowstonePlanner(scan_local_devices(), planner_version=settings.yellowstone_planner_version, policy_version=settings.yellowstone_policy_version, local_preprocess_enabled=settings.local_preprocess_enabled, local_student_enabled=settings.local_student_enabled)
    plan = planner.plan(model=payload.model, traffic=TrafficProfile(raw_tokens, ys_tokens, final_tokens, p95_output_tokens=payload.p95_output_tokens, workload_class=payload.workload_class), compression_mode=payload.yscompress.get("mode", settings.yscompress_default_mode), prompt_cache_mode=payload.prompt_cache)
    return plan.to_dict()


@app.post("/v1/deployments")
def create_deployment(payload: DeploymentCreate, request: Request) -> dict[str, Any]:
    principal = _auth(request, "deployments:write")
    return store.create_deployment(project_id=principal.project_id, name=payload.name, plan=payload.plan)


@app.get("/v1/deployments")
def list_deployments(request: Request) -> list[dict[str, Any]]:
    principal = _auth(request, "deployments:read")
    return store.list_deployments(project_id=principal.project_id)


@app.get("/v1/ccr/{ccr_id}")
def get_ccr(ccr_id: str, request: Request) -> dict[str, Any]:
    principal = _auth(request, "ccr:read")
    item = store.get_ccr(project_id=principal.project_id, ccr_id=ccr_id, crypto=crypto, include_payload=True)
    store.record_audit(project_id=principal.project_id, actor_key_prefix=principal.key_prefix, action="ccr.read", target_type="ccr", target_id=ccr_id, request_id=current_request_id())
    if not item:
        raise HTTPException(status_code=404, detail="CCR item not found")
    return item


@app.delete("/v1/ccr/{ccr_id}")
def delete_ccr(ccr_id: str, request: Request) -> dict[str, Any]:
    principal = _auth(request, "ccr:delete")
    assert_csrf_for_browser_mutation(request)
    ok = store.delete_ccr(project_id=principal.project_id, ccr_id=ccr_id)
    store.record_audit(project_id=principal.project_id, actor_key_prefix=principal.key_prefix, action="ccr.delete", target_type="ccr", target_id=ccr_id, request_id=current_request_id())
    if not ok:
        raise HTTPException(status_code=404, detail="CCR item not found")
    return {"ok": True, "deleted": ccr_id}


@app.post("/v1/ccr/purge_expired")
def purge_expired_ccr(request: Request) -> dict[str, Any]:
    # purge_expired deletes across ALL projects — it is a platform-level maintenance
    # operation, not a per-project one. Require admin, not just ccr:delete.
    principal = _auth(request, "admin")
    n = store.purge_expired_ccr()
    store.record_audit(project_id=principal.project_id, actor_key_prefix=principal.key_prefix, action="ccr.purge_expired", request_id=current_request_id(), metadata={"count": n})
    return {"ok": True, "purged": n}


@app.get("/v1/usage")
def usage(request: Request) -> dict[str, Any]:
    principal = _auth(request, "usage:read")
    return store.usage_summary(project_id=principal.project_id)


@app.get("/v1/limits")
def get_limits(request: Request) -> dict[str, Any]:
    """The caller's own effective quota + current consumption (powers `decompute limits`).

    Unlike /v1/projects/{id}/limits (admin, any project), this is scoped to the key's own
    project so a normal user can see why they might be throttled.
    """
    principal = _auth(request, "usage:read")
    metered = rate_limits._metered(principal)
    limits = rate_limits.limits_for(principal)  # unmetered for local/self-hosted/BYOK
    return {
        "project_id": principal.project_id,
        "tier": store.get_project_tier(principal.project_id) if metered else "unmetered",
        "metered": metered,
        "limits": limits,
        "monthly_spend_usd": store.monthly_spend(project_id=principal.project_id),
        "monthly_spend_cap_usd": limits.get("monthly_spend_usd") if metered else None,
    }


# ── Billing / licensing ───────────────────────────────────────────────────────
def _device_hash(request: Request, body: dict[str, Any] | None = None) -> str | None:
    """Server-derived device id: HMAC of the client-supplied fingerprint (header or body). The raw
    fingerprint is never stored or trusted as an identity — only this digest is used."""
    fp = device_fingerprint_from_request(request.headers, body)
    return store.hash_device_fingerprint(fp) if fp else None


@app.get("/v1/billing/status")
def billing_status(request: Request) -> dict[str, Any]:
    principal = _auth(request, "usage:read")
    info = store.get_project_billing(principal.project_id) or {"project_id": principal.project_id, "tier": "free"}
    info["billing_enabled"] = billing.enabled
    info["metered"] = rate_limits._metered(principal)
    info["limits"] = rate_limits.limits_for(principal)
    # Merge device-license info when the caller presents a fingerprint (BYOK axis). Best-effort:
    # never let the licensing subsystem break billing status (e.g. migration not yet applied).
    dh = _device_hash(request)
    if dh:
        try:
            info["device_license"] = store.get_device_license_status(dh)
        except Exception:
            pass
    return info


@app.post("/v1/billing/checkout")
async def billing_checkout(request: Request) -> JSONResponse:
    """Start a subscription checkout; returns the Stripe Checkout URL.

    If the caller presents a device fingerprint (BYOK/self-hosted), the issued license binds to
    that device; otherwise it's a hosted project-tier subscription."""
    principal = _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        result = billing.create_checkout_session(
            project_id=principal.project_id, customer_email=(body or {}).get("email"),
            device_hash=_device_hash(request),
        )
    except BillingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse({"checkout_url": result["url"], "session_id": result["id"]})


# ── Device licensing (BYOK axis) ──────────────────────────────────────────────
@app.post("/v1/billing/redeem-checkout")
async def billing_redeem_checkout(request: Request) -> JSONResponse:
    """Verify a PAID Checkout Session and issue a device Pro license (once). Body: {session_id}.
    If a device fingerprint is present, the license is activated for this device immediately."""
    principal = _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = (body or {}).get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        result = billing.redeem_checkout(session_id=session_id, device_hash=_device_hash(request))
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(result)


@app.post("/v1/licenses/activate")
async def licenses_activate(request: Request) -> JSONResponse:
    """Bind an existing license key to THIS device. Body: {license_key}; device via header."""
    _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    dh = _device_hash(request)
    if not dh:
        raise HTTPException(status_code=400, detail=f"a device fingerprint ({DEVICE_HEADER}) is required to activate")
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body or {}).get("license_key")
    if not key:
        raise HTTPException(status_code=400, detail="license_key is required")
    res = store.activate_device_binding(raw_key=key, device_hash=dh)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=f"could not activate license: {res.get('reason')}")
    return JSONResponse({"activated": True, "tier": res["tier"], "current_period_end": res.get("current_period_end"), "device_hash_prefix": dh[:12]})


@app.get("/v1/licenses/status")
def licenses_status(request: Request) -> dict[str, Any]:
    """Device-scoped license + free-quota status (no quota consumption). Powers `decompute license`."""
    _auth(request, "usage:read")
    dh = _device_hash(request)
    if not dh:
        return {"tier": "free", "license_status": "none", "device": False}
    return {**store.get_device_license_status(dh), "device": True}


@app.post("/v1/licenses/deactivate")
async def licenses_deactivate(request: Request) -> JSONResponse:
    """Release this device's license binding so the seat can move to another device."""
    _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    dh = _device_hash(request)
    if not dh:
        raise HTTPException(status_code=400, detail=f"a device fingerprint ({DEVICE_HEADER}) is required")
    n = store.release_device_binding(device_hash=dh)
    return JSONResponse({"released": n})


# ── Desktop-app license contract (boss spec): rich status + modal actions ─────
@app.get("/v1/license/status")
def license_status_v1(request: Request) -> dict[str, Any]:
    """Device-scoped license + free-quota decision in the full UI contract (no consumption).
    Powers the desktop upgrade modal. Falls back to a free/no-fingerprint shape."""
    _auth(request, "usage:read")
    fp = device_fingerprint_from_request(request.headers, {})
    meta = licensing.resolve(fp, consume=False) if fp else None
    if not meta:
        return {"plan": "free", "license_status": "none", "device": False,
                "ui": {"show_modal": False, "actions": []}}
    out = LicenseService.ui_metadata(meta)
    out["device"] = True
    out["headers"] = LicenseService.decision_headers(meta)
    return out


@app.post("/v1/license/continue_without_pro")
async def license_continue_without_pro(request: Request) -> JSONResponse:
    """User dismissed the upgrade modal — acknowledge and keep compression paused until reset.
    (In the per-request degrade model the quota is already spent; this just returns the state.)"""
    _auth(request, "usage:read")
    try:
        body = await request.json()
    except Exception:
        body = {}
    fp = device_fingerprint_from_request(request.headers, body)
    meta = licensing.resolve(fp, consume=False) if fp else None
    return JSONResponse({"ok": True, "status": LicenseService.ui_metadata(meta) if meta else {}})


@app.post("/v1/license/activate")
async def license_activate_v1(request: Request) -> JSONResponse:
    """Boss-spec activation path: bind a license key to this device (header or body fingerprint)."""
    _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    dh = _device_hash(request, body)
    if not dh:
        raise HTTPException(status_code=400, detail="a device fingerprint is required to activate")
    key = (body or {}).get("license_key")
    if not key:
        raise HTTPException(status_code=400, detail="license_key is required")
    res = store.activate_device_binding(raw_key=key, device_hash=dh)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=f"could not activate license: {res.get('reason')}")
    return JSONResponse({"ok": True, "tier": res["tier"], "current_period_end": res.get("current_period_end"),
                         "device_hash_prefix": dh[:12]})


@app.post("/v1/admin/licenses/issue")
async def admin_issue_license(request: Request) -> JSONResponse:
    """Admin/dev: mint a Pro license WITHOUT Stripe (manual grant / demo / website-redeem backend).
    OFF by default (DECOMPUTE_ENABLE_ADMIN_LICENSE_ISSUE) so it can't be a free-Pro bypass on a
    self-hosted/local_noauth gateway; also requires an admin scope. The raw key is returned once."""
    if not settings.enable_admin_license_issue:
        raise HTTPException(status_code=403, detail="admin license issuance is disabled (set DECOMPUTE_ENABLE_ADMIN_LICENSE_ISSUE=true to enable)")
    _auth(request, "keys:admin")
    assert_csrf_for_browser_mutation(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = store.issue_license(
        stripe_checkout_session_id=None, stripe_customer_id=None, stripe_subscription_id=None,
        tier=(body or {}).get("plan") or "pro", current_period_end=None,
        device_hash=_device_hash(request, body),  # bind immediately if a fingerprint is present
    )
    return JSONResponse({"ok": True, "license_key": res.get("license_key"), "license_id": res.get("id"),
                         "tier": res.get("tier", "pro")})


@app.get("/v1/admin/licenses")
def admin_list_licenses(request: Request) -> dict[str, Any]:
    """Admin: list issued licenses (hashes/prefixes only — no raw keys)."""
    _auth(request, "keys:admin")
    return {"licenses": store.list_licenses()}


@app.post("/v1/billing/portal")
async def billing_portal(request: Request) -> JSONResponse:
    """Open the Stripe billing portal for the caller's project (manage/cancel)."""
    principal = _auth(request, "messages:write")
    assert_csrf_for_browser_mutation(request)
    try:
        result = billing.create_portal_session(project_id=principal.project_id)
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"portal_url": result["url"]})


@app.post("/v1/billing/webhook")
@app.post("/v1/billing/stripe-webhook")  # boss-spec name; same handler, signature-verified
async def billing_webhook(request: Request) -> JSONResponse:
    """Stripe webhook — the ONLY path that grants/revokes a license/tier. No auth; signature-verified.

    Tier/license changes never come from a client request; they come from a signature-verified
    Stripe event keyed back to the project (client_reference_id) or device (metadata.device_hash).
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = billing.construct_event(payload, sig)
    except BillingError as exc:
        # Bad/forged signature, or no webhook secret configured.
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = billing.handle_event(event)
    except Exception as exc:
        log.error("billing webhook handler failed", extra={"extra": {"type": event.get("type"), "error": str(exc)}})
        raise HTTPException(status_code=500, detail="webhook processing failed")
    if result.get("action") in {"granted", "revoked", "synced"} and result.get("project_id"):
        try:
            store.record_audit(project_id=result["project_id"], actor_key_prefix=None,
                               action=f"billing.{result['action']}", target_type="subscription",
                               target_id=result.get("project_id"), metadata={k: result.get(k) for k in ("tier", "status", "type")})
        except Exception:
            pass
    return JSONResponse({"received": True, **{k: result.get(k) for k in ("handled", "action")}})


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid ISO date: {value!r}")


@app.get("/v1/usage/export")
def usage_export(request: Request, format: str = "csv", start: str | None = None, end: str | None = None) -> Response:
    principal = _auth(request, "usage:read")
    rows = store.export_usage(project_id=principal.project_id, start=_parse_iso(start), end=_parse_iso(end))
    if format == "json":
        return JSONResponse(rows)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
    else:
        buf.write("id,project_id,created_at\n")
    return Response(buf.getvalue(), media_type="text/csv", headers={"content-disposition": "attachment; filename=decompute_usage.csv"})


@app.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    _auth(request, "messages:write")
    result = await list_anthropic_models(settings=settings, upstream_api_key=upstream_anthropic_key(request, settings), extra_headers=dict(request.headers))
    return JSONResponse(result.body, status_code=result.status_code, headers=_response_headers(request_id=current_request_id(), upstream_request_id=result.upstream_request_id))


@app.post("/v1/messages/count_tokens")
async def messages_count_tokens(request: Request) -> JSONResponse:
    _auth(request, "messages:write")
    body = await request.json()
    result = await count_tokens(body, settings=settings, upstream_api_key=upstream_anthropic_key(request, settings), extra_headers=dict(request.headers))
    return JSONResponse(result.body, status_code=result.status_code, headers=_response_headers(request_id=current_request_id(), upstream_request_id=result.upstream_request_id))


def _check_model_allowed(model: str, principal: AuthPrincipal) -> None:
    """Enforce both the global gateway allowlist and the per-project allowlist."""
    if not settings.model_allowed(model):
        raise HTTPException(status_code=403, detail=f"Model {model!r} is not allowed on this gateway")
    # Per-project allowlist (stored in DB at project creation time).
    # Only enforced for verified keys — local_noauth and dev bypass this.
    if principal.auth_mode == "api_key":
        project = store.get_project(principal.project_id)
        if project and project.get("allowed_models"):
            lower = model.lower()
            if not any(m.lower() in lower for m in project["allowed_models"]):
                raise HTTPException(
                    status_code=403,
                    detail=f"Model {model!r} is not in the allowed list for project {principal.project_id}",
                )


@app.post("/v1/messages")
async def anthropic_messages_proxy(request: Request, payload: MessagesRequest) -> Response:
    request_id = current_request_id()
    session_id = _clean_session_id(request.headers.get("x-decompute-session-id"))
    principal = _auth(request, "messages:write")
    start = time.perf_counter()
    body = payload.model_dump(exclude_none=True, mode="python")
    # Ensure messages are plain dicts for downstream compression + passthrough.
    body["messages"] = [m if isinstance(m, dict) else m.model_dump() for m in payload.messages]
    model = body.get("model", "claude")
    _check_model_allowed(model, principal)
    decompute_cfg = body.get("decompute") or {}
    ys_cfg = decompute_cfg.get("yscompress") or decompute_cfg.get("headroom") or {}
    mode = ys_cfg.get("mode", settings.yscompress_default_mode)
    if request.headers.get("x-yscompress-bypass", "false").lower() == "true" or request.headers.get("x-headroom-bypass", "false").lower() == "true":
        mode = "off"
    # Device licensing (BYOK/self-hosted axis): only for NON-metered callers — hosted dc_ tenants
    # keep their project-tier path untouched. A device's active Pro license keeps compression on;
    # a free device spends one daily quota unit, and once exhausted compression degrades to
    # mode='off' (the request still reaches Claude, just uncompressed).
    license_meta = None
    if not rate_limits._metered(principal):
        license_meta = licensing.resolve(device_fingerprint_from_request(request.headers, body))
        if license_meta and not license_meta["compress"]:
            mode = "off"
    raw_estimate = estimate_tokens(body.get("messages")) + estimate_tokens(body.get("system")) + estimate_tokens(body.get("tools"))
    # COST GUARD — preserve prompt caching, but ONLY for very large contexts. When the client manages
    # prompt caching (Claude Code sends cache_control), compressing rewrites the cached prefix and
    # busts the cache. For a HUGE repeated context (e.g. a ~700K-token Opus session) the cache discount
    # (~90% off repeated input) dwarfs compression savings, so we skip compression and let the cache
    # work (this is what fixed the ~$10/turn re-bills). But for normal requests — a fresh log read, a
    # few tool results — the NEW content compresses ~90-99% while the cache being protected is small,
    # so compression is the bigger win and we MUST still compress. The threshold separates the two:
    # below it, compress; above it, preserve the cache.
    cache_preserved = False
    if (mode != "off" and settings.preserve_prompt_cache
            and raw_estimate >= settings.preserve_cache_min_tokens
            and _request_has_cache_control(body)):
        mode = "off"
        cache_preserved = True
    rate_limits.check_body_size(request.headers.get("content-length"))
    rate_limits.check_preflight(principal=principal, estimated_input_tokens=raw_estimate, estimated_output_tokens=int(body.get("max_tokens", 1024)), store=store)
    compress_cache_control_large_blocks = (
        mode != "off"
        and not cache_preserved
        and _request_has_cache_control(body)
    )
    comp = await yscompress.compress_messages(
        body.get("messages") or [],
        project_id=principal.project_id,
        mode=mode,
        ccr=ys_cfg.get("ccr", settings.ccr_enabled),
        compress_cache_control_large_blocks=compress_cache_control_large_blocks,
    )
    compressed_system = body.get("system")
    system_before = system_after = 0
    system_transforms: list[str] = []
    if compressed_system is not None:
        compressed_system, system_before, system_after, system_transforms = await yscompress.compress_system(
            compressed_system,
            mode=mode,
        )
        comp.tokens_before += system_before
        comp.tokens_after += system_after
        comp.tokens_saved = max(0, comp.tokens_before - comp.tokens_after)
        comp.compression_ratio = round(comp.tokens_after / max(comp.tokens_before, 1), 4)
        if system_transforms:
            comp.transforms_applied.extend(system_transforms)
    workload_inputs = list(body.get("messages") or [])
    if body.get("system") is not None:
        workload_inputs.append({"role": "system", "content": body.get("system")})
    workload = classify_workload(workload_inputs)
    compressed_body = dict(body)
    compressed_body["messages"] = comp.messages
    if compressed_system is not None:
        compressed_body["system"] = compressed_system
    # When CCR elided content behind references, register the retrieve tool so Claude
    # can fetch originals on demand. Append without disturbing customer-set tools.
    if comp.ccr_keys:
        existing_tools = list(compressed_body.get("tools") or [])
        if not any(isinstance(t, dict) and t.get("name") == CCR_RETRIEVE_TOOL["name"] for t in existing_tools):
            existing_tools.append(CCR_RETRIEVE_TOOL)
        compressed_body["tools"] = existing_tools
    prompt_cache_mode = decompute_cfg.get("prompt_cache", settings.prompt_cache_mode)
    planner = YellowstonePlanner(scan_local_devices(), planner_version=settings.yellowstone_planner_version, policy_version=settings.yellowstone_policy_version, local_preprocess_enabled=settings.local_preprocess_enabled, local_student_enabled=settings.local_student_enabled)
    plan = planner.plan(model=model, traffic=TrafficProfile(comp.tokens_before, comp.tokens_after, comp.tokens_after, p95_output_tokens=body.get("max_tokens", 1024), workload_class=decompute_cfg.get("workload_class", "interactive")), compression_mode=mode, prompt_cache_mode=prompt_cache_mode)
    upstream_key = upstream_anthropic_key(request, settings)
    headers = _response_headers(request_id=request_id, comp=comp, plan=plan)
    _apply_license_headers(headers, license_meta)
    if cache_preserved:
        headers["x-decompute-prompt-cache-preserved"] = "true"  # compression skipped to keep the client's cache hits
    if bool(body.get("stream")):
        # Streaming with client-disconnect cleanup. proxy_sse_stream polls the
        # downstream client; on disconnect/cancel it closes the upstream Anthropic
        # stream and runs on_complete exactly once so usage is always finalized.
        # `legacy` is fed by our provider only to capture the upstream request-id
        # (an HTTP header the byte-parsing tracker can't see).
        legacy = SSEUsageTracker()
        stream_tracker = StreamUsageTracker()
        # Real-tokenizer baseline of the uncompressed prompt, fired concurrently with the stream
        # (which runs for seconds) so it is ready at completion and adds no latency. _safe_complete
        # awaits the async recorder in a shielded task, so this survives a client disconnect.
        baseline_task = asyncio.create_task(_real_token_baseline(model, body, upstream_key=upstream_key))

        async def record_stream_usage(done: StreamUsageTracker) -> None:
            done.upstream_request_id = done.upstream_request_id or legacy.upstream_request_id
            if done.cancelled:
                status_code = 499
            elif done.stream_error:
                # Prefer the real upstream HTTP status (429/500/529/...) when the
                # provider propagated it; fall back to a generic 502 otherwise.
                status_code = done.upstream_status_code or 502
            else:
                status_code = 200
            if done.cancelled:
                log.info("stream cancelled", extra={"extra": {"request_id": request_id, "reason": done.cancel_reason}})
            # Reconcile to Anthropic's BILLED input (fixed at message_start) on the real tokenizer.
            # A stream that errored/cancelled before message_start has input_tokens==0 -> keep the
            # estimate, never a phantom 100% savings.
            billed_input = real_raw = None
            if status_code == 200 and done.input_tokens > 0:
                billed_input = done.input_tokens
                try:
                    real_raw = await baseline_task
                except Exception:
                    real_raw = None
            else:
                baseline_task.cancel()
            _record_usage(
                principal=principal, endpoint="/v1/messages:stream", model=model,
                raw_input_tokens=comp.tokens_before, yscompress_input_tokens=comp.tokens_after,
                yellowstone_final_input_tokens=plan.yellowstone_final_input_tokens,
                output_tokens=done.output_tokens,
                cache_creation_input_tokens=done.cache_creation_input_tokens,
                cache_read_input_tokens=done.cache_read_input_tokens,
                prompt_cache_mode=prompt_cache_mode, plan=plan, request_id=request_id,
                upstream_request_id=done.upstream_request_id, status_code=status_code,
                start_time=start, workload_class=workload, session_id=session_id,
                stream_status=done.status, stream_cancelled=done.cancelled,
                stream_cancel_reason=done.cancel_reason, stream_json=done.to_dict(),
                billed_input_tokens=billed_input, real_raw_input_tokens=real_raw,
            )

        async def body_iter(release_slot):
            upstream = stream_anthropic_messages(
                compressed_body, settings=settings, upstream_api_key=upstream_key,
                prompt_cache_mode=prompt_cache_mode, tracker=legacy, extra_headers=dict(request.headers),
            )
            # The slot is released on every exit — normal completion, client
            # disconnect, or task cancellation — via this generator's finally,
            # which Starlette runs when it closes the response body.
            try:
                async for chunk in proxy_sse_stream(
                    upstream, request=request, tracker=stream_tracker, on_complete=record_stream_usage,
                ):
                    yield chunk
            finally:
                release_slot()

        # Reserve the concurrent-stream slot here, in the request cycle, so an
        # over-limit raises a real HTTP 429 (a limit enforced inside body_iter would
        # fire only after the 200 status line is already committed on the wire).
        release_slot = rate_limits.acquire_stream_slot(principal.project_id)
        return StreamingResponse(body_iter(release_slot), media_type="text/event-stream", headers=headers)
    # Real-tokenizer baseline of the UNCOMPRESSED prompt, fired CONCURRENTLY with generation so
    # it adds ~0 wall-time. Started for every request; only USED when no CCR retrieval re-sent
    # originals (see below) — including CCR requests where Claude never retrieved, which is the
    # common high-savings case the estimator most overstated.
    baseline_task = asyncio.create_task(_real_token_baseline(model, body, upstream_key=upstream_key))
    result = await call_anthropic_messages(compressed_body, settings=settings, upstream_api_key=upstream_key, prompt_cache_mode=prompt_cache_mode, extra_headers=dict(request.headers))
    # Honest accounting: CCR savings only materialize if Claude does NOT retrieve.
    # When it does, the originals come back and must be counted as tokens sent.
    final_input_tokens = plan.yellowstone_final_input_tokens
    ccr_rounds = 0
    if comp.ccr_keys:
        result, ccr_rounds, retrieved_tokens = await _resolve_ccr_retrievals(result, compressed_body, principal=principal, upstream_key=upstream_key, prompt_cache_mode=prompt_cache_mode, request=request)
        if ccr_rounds:
            headers["x-decompute-ccr-retrieve-rounds"] = str(ccr_rounds)
            # Add what retrieval re-sent; cap at raw so savings floor at 0, never negative.
            final_input_tokens = min(comp.tokens_before, final_input_tokens + retrieved_tokens)
            headers["x-decompute-tokens-after"] = str(final_input_tokens)
            headers["x-decompute-tokens-saved"] = str(max(0, comp.tokens_before - final_input_tokens))
    usage = _usage_from_result(result)
    # Reconcile recorded savings to Anthropic's BILLED input on the real tokenizer. Skip ONLY when
    # retrieval re-sent originals across rounds (ccr_rounds>0) — there billed reflects just the last
    # round and would undercount, so keep the estimate+retrieved accounting (documented follow-up).
    billed_input = real_raw = None
    if ccr_rounds == 0 and result.status_code < 400 and usage["input_tokens"] > 0:
        billed_input = usage["input_tokens"]
        try:
            real_raw = await baseline_task
        except Exception:
            real_raw = None
    else:
        baseline_task.cancel()  # CCR-with-retrieval / error / empty body -> keep the estimate
    _record_usage(principal=principal, endpoint="/v1/messages", model=model, raw_input_tokens=comp.tokens_before, yscompress_input_tokens=comp.tokens_after, yellowstone_final_input_tokens=final_input_tokens, output_tokens=usage["output_tokens"], cache_creation_input_tokens=usage["cache_creation_input_tokens"], cache_read_input_tokens=usage["cache_read_input_tokens"], prompt_cache_mode=prompt_cache_mode, plan=plan, request_id=request_id, upstream_request_id=result.upstream_request_id, status_code=result.status_code, start_time=start, workload_class=workload, session_id=session_id, billed_input_tokens=billed_input, real_raw_input_tokens=real_raw)
    body_out = result.body
    if (settings.return_proxy_metadata or request.headers.get("x-decompute-metadata", "false").lower() == "true") and isinstance(body_out, dict):
        body_out = dict(body_out)
        body_out["decompute"] = {"yscompress": comp.to_dict(), "yellowstone": plan.to_dict(), "request_id": request_id, "upstream_request_id": result.upstream_request_id}
        if license_meta:
            body_out["decompute"]["license"] = LicenseService.ui_metadata(license_meta)
    headers.update(_response_headers(request_id=request_id, comp=comp, plan=plan, upstream_request_id=result.upstream_request_id))
    # Re-apply the CCR-adjusted token counts AFTER _response_headers (which regenerates
    # them from the pre-retrieval plan) so the header matches what was actually sent.
    headers["x-decompute-tokens-after"] = str(final_input_tokens)
    headers["x-decompute-tokens-saved"] = str(max(0, comp.tokens_before - final_input_tokens))
    return JSONResponse(body_out, status_code=result.status_code, headers=headers)


@app.post("/v1/chat/completions")
async def openai_chat_proxy(request: Request) -> JSONResponse:
    principal = _auth(request, "messages:write")
    session_id = _clean_session_id(request.headers.get("x-decompute-session-id"))
    start = time.perf_counter()
    body = await request.json()
    model = body.get("model", "claude-sonnet-4-5")
    _check_model_allowed(model, principal)
    messages = body.get("messages") or []
    system_parts = [m.get("content") for m in messages if m.get("role") == "system"]
    anthropic_messages = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            anthropic_messages.append({"role": "user", "content": f"Tool output:\n{m.get('content','')}"})
        elif role in {"assistant", "user"}:
            anthropic_messages.append({"role": role, "content": m.get("content", "")})
        else:
            anthropic_messages.append({"role": "user", "content": f"{role}: {m.get('content','')}"})
    raw_estimate = estimate_tokens(anthropic_messages) + estimate_tokens(system_parts)
    rate_limits.check_preflight(principal=principal, estimated_input_tokens=raw_estimate, estimated_output_tokens=int(body.get("max_tokens", 1024)), store=store)
    mode = ((body.get("decompute") or {}).get("yscompress") or {}).get("mode", settings.yscompress_default_mode)
    # Device-licensing gate (BYOK axis) — mirrors /v1/messages; non-metered callers only.
    if not rate_limits._metered(principal):
        lm = licensing.resolve(request.headers.get(DEVICE_HEADER))
        if lm and not lm["compress"]:
            mode = "off"
    comp = await yscompress.compress_messages(anthropic_messages, project_id=principal.project_id, mode=mode, ccr=settings.ccr_enabled)
    workload = classify_workload(anthropic_messages)
    planner = YellowstonePlanner(scan_local_devices(), planner_version=settings.yellowstone_planner_version, policy_version=settings.yellowstone_policy_version)
    plan = planner.plan(model=model, traffic=TrafficProfile(comp.tokens_before, comp.tokens_after, comp.tokens_after, p95_output_tokens=body.get("max_tokens", 1024)), compression_mode=mode, prompt_cache_mode=settings.prompt_cache_mode)
    upstream_body = {"model": model, "max_tokens": body.get("max_tokens", 1024), "system": "\n".join(str(s) for s in system_parts if s), "messages": comp.messages}
    result = await call_anthropic_messages(upstream_body, settings=settings, upstream_api_key=upstream_anthropic_key(request, settings), prompt_cache_mode=settings.prompt_cache_mode, extra_headers=dict(request.headers))
    text_out = ""
    if isinstance(result.body, dict):
        for block in result.body.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_out += block.get("text", "")
    usage = _usage_from_result(result)
    _record_usage(principal=principal, endpoint="/v1/chat/completions", model=model, raw_input_tokens=comp.tokens_before, yscompress_input_tokens=comp.tokens_after, yellowstone_final_input_tokens=plan.yellowstone_final_input_tokens, output_tokens=usage["output_tokens"] or estimate_tokens(text_out), cache_creation_input_tokens=usage["cache_creation_input_tokens"], cache_read_input_tokens=usage["cache_read_input_tokens"], prompt_cache_mode=settings.prompt_cache_mode, plan=plan, request_id=current_request_id(), upstream_request_id=result.upstream_request_id, status_code=result.status_code, start_time=start, workload_class=workload, session_id=session_id)
    if result.status_code >= 400:
        return JSONResponse(result.body, status_code=result.status_code)
    return JSONResponse({"id": result.body.get("id", "chatcmpl_mock"), "object": "chat.completion", "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": text_out}, "finish_reason": result.body.get("stop_reason", "stop")}], "usage": {"prompt_tokens": comp.tokens_after, "completion_tokens": usage["output_tokens"], "total_tokens": comp.tokens_after + usage["output_tokens"], "raw_prompt_tokens": comp.tokens_before, "tokens_saved": comp.tokens_saved}, "yellowstone": plan.to_dict(), "yscompress": comp.to_dict()}, status_code=200)


@app.post("/v1/local/preprocess")
async def local_preprocess_placeholder(request: Request) -> dict[str, Any]:
    principal = _auth(request, "messages:write")
    if not settings.local_preprocess_enabled:
        return {"enabled": False, "route": "local_preprocess_disabled", "message": "Local preprocessing is a V0 placeholder and is disabled by default."}
    body = await request.json()
    return {"enabled": True, "route": "local_preprocess_then_claude", "messages": body.get("messages") or []}


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Decompute Gateway is running", "dashboard": "/dashboard", "docs": "/docs"}


def _support_auth_ctx(request: Request) -> dict[str, Any]:
    """Adapt this branch's AuthPrincipal into the dict the support router expects.

    Requires support:read; admin keys satisfy it (AuthPrincipal.has_scope treats
    'admin' as a wildcard), and dev/local_noauth get admin scopes by default.
    A non-admin key without the scope is rejected by _auth before we get here.
    """
    p = _auth(request, "support:read")
    return {
        "auth_mode": p.auth_mode,
        "is_admin": "admin" in p.scopes,
        "scopes": list(p.scopes),
        "project_id": p.project_id,
        "key_prefix": p.key_prefix,
    }


def _support_user_ctx(request: Request) -> dict[str, Any]:
    """Plain authenticated principal — any valid key may upload ITS OWN support bundle."""
    p = _auth(request)
    return {"auth_mode": p.auth_mode, "project_id": p.project_id, "key_prefix": p.key_prefix}


# Support bundle endpoints: create/report are admin/support scope; upload is any user (own bundle).
include_support_bundle_router(
    app,
    store=store,
    settings=settings,
    auth_context_func=_support_auth_ctx,
    capabilities_func=lambda: capabilities_dict(settings),
    health_func=health,
    user_auth_func=_support_user_ctx,
)
