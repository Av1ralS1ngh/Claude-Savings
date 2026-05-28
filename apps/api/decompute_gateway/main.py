from __future__ import annotations

from typing import Any
import html
import json

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .anthropic_provider import call_anthropic_messages, stream_anthropic_messages
from .config import get_settings
from .db import Store
from .yscompress import YSCompressClient, estimate_tokens
from .security import auth_context, upstream_anthropic_key
from .yellowstone import YellowstonePlanner, TrafficProfile, scan_local_devices

settings = get_settings()
store = Store(settings.sqlite_path)
yscompress = YSCompressClient(settings, store)

app = FastAPI(
    title="Decompute Claude Optimization Gateway",
    version="0.1.0",
    description="YSCompress + Yellowstone gateway for Claude token/compute optimization.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProjectCreate(BaseModel):
    name: str


class ApiKeyCreate(BaseModel):
    name: str = "default"
    project_id: str = "proj_default"


class CompressionPreview(BaseModel):
    messages: list[dict[str, Any]]
    mode: str = "safe"
    ccr: bool = True


class RoutePlanRequest(BaseModel):
    model: str = "claude-sonnet-4-5"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    raw_input_tokens: int | None = None
    compressed_input_tokens: int | None = None
    p95_output_tokens: int = 1024
    yscompress: dict[str, Any] = Field(default_factory=dict)
    prompt_cache: str = "auto"


class DeploymentCreate(BaseModel):
    name: str
    plan: dict[str, Any]


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": settings.app_name,
        "environment": settings.environment,
        "mock_anthropic": settings.mock_anthropic,
        "yscompress": bool(settings.yscompress_base_url),
        "prompt_cache_mode": settings.prompt_cache_mode,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    usage = store.usage_summary()
    devices = store.list_devices() or [d.to_dict() for d in scan_local_devices()]
    deployments = store.list_deployments()
    keys = store.list_api_keys()
    return f"""
<!doctype html>
<html>
<head>
  <title>Decompute Gateway</title>
  <style>
    body {{ font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; color: #111827; background: #f9fafb; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 14px; padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
    code, pre {{ background: #111827; color: #f9fafb; border-radius: 10px; padding: 12px; overflow: auto; display: block; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }}
    th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #e5e7eb; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
  <h1>Decompute Gateway</h1>
  <p class="muted">Claude optimization through YSCompress context compression and Yellowstone routing.</p>
  <div class="grid">
    <div class="card"><b>Requests</b><h2>{usage['requests']}</h2></div>
    <div class="card"><b>Raw input tokens</b><h2>{usage['raw_input_tokens']}</h2></div>
    <div class="card"><b>Sent input tokens</b><h2>{usage['compressed_input_tokens']}</h2></div>
    <div class="card"><b>Estimated savings</b><h2>{usage['estimated_savings_percent']:.1f}%</h2></div>
  </div>

  <h2>Local setup</h2>
  <pre>export DECOMPUTE_BASE_URL=http://127.0.0.1:8080
export DECOMPUTE_API_KEY=dc_dev_local
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
# For real Claude upstream:
# export ANTHROPIC_API_KEY=sk-ant-...
# export DECOMPUTE_MOCK_ANTHROPIC=false</pre>

  <h2>Devices</h2>
  {render_table(devices)}
  <h2>Deployments</h2>
  {render_table(deployments)}
  <h2>API keys</h2>
  {render_table(keys)}
</body>
</html>
"""


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>None yet.</p>"
    # Flatten nested plan/profile summaries.
    normalized = []
    for r in rows:
        out = {}
        for k, v in r.items():
            if isinstance(v, (dict, list)):
                out[k] = html.escape(json.dumps(v)[:160])
            else:
                out[k] = html.escape(str(v))
        normalized.append(out)
    cols = list(normalized[0].keys())[:8]
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{row.get(c,'')}</td>" for c in cols) + "</tr>" for row in normalized)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@app.post("/v1/projects")
def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    return store.create_project(payload.name)


@app.get("/v1/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    auth_context(request, store, settings)
    return store.list_projects()


@app.post("/v1/api_keys")
def create_api_key(payload: ApiKeyCreate, request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    return store.create_api_key(payload.name, payload.project_id)


@app.get("/v1/api_keys")
def list_api_keys(request: Request) -> list[dict[str, Any]]:
    auth_context(request, store, settings)
    return store.list_api_keys()


@app.post("/v1/devices/scan")
def scan_devices(request: Request) -> list[dict[str, Any]]:
    ctx = auth_context(request, store, settings)
    profiles = [d.to_dict() for d in scan_local_devices()]
    for p in profiles:
        store.upsert_device(p, project_id=ctx["project_id"])
    return profiles


@app.get("/v1/devices")
def list_devices(request: Request) -> list[dict[str, Any]]:
    auth_context(request, store, settings)
    return store.list_devices()


@app.post("/v1/compression/preview")
async def compression_preview(payload: CompressionPreview, request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    result = await yscompress.compress_messages(payload.messages, mode=payload.mode, ccr=payload.ccr)
    return result.to_dict()


@app.post("/v1/routes/plan")
async def route_plan(payload: RoutePlanRequest, request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    devices = [d.to_dict() for d in scan_local_devices()]
    for d in devices:
        store.upsert_device(d)
    if payload.raw_input_tokens is not None and payload.compressed_input_tokens is not None:
        raw_tokens = payload.raw_input_tokens
        compressed_tokens = payload.compressed_input_tokens
    else:
        comp = await yscompress.compress_messages(
            payload.messages,
            mode=payload.yscompress.get("mode", settings.yscompress_default_mode),
            ccr=payload.yscompress.get("ccr", True),
        )
        raw_tokens = comp.tokens_before
        compressed_tokens = comp.tokens_after
    planner = YellowstonePlanner(scan_local_devices())
    plan = planner.plan(
        model=payload.model,
        traffic=TrafficProfile(
            raw_input_tokens=raw_tokens,
            compressed_input_tokens=compressed_tokens,
            p95_output_tokens=payload.p95_output_tokens,
        ),
        compression_mode=payload.yscompress.get("mode", settings.yscompress_default_mode),
        prompt_cache_mode=payload.prompt_cache,
    )
    return plan.to_dict()


@app.post("/v1/deployments")
def create_deployment(payload: DeploymentCreate, request: Request) -> dict[str, Any]:
    ctx = auth_context(request, store, settings)
    return store.create_deployment(payload.name, payload.plan, ctx["project_id"])


@app.get("/v1/deployments")
def list_deployments(request: Request) -> list[dict[str, Any]]:
    auth_context(request, store, settings)
    return store.list_deployments()


@app.get("/v1/ccr/{ccr_id}")
def get_ccr(ccr_id: str, request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    item = store.get_ccr(ccr_id)
    if not item:
        raise HTTPException(status_code=404, detail="CCR item not found")
    return item


@app.get("/v1/usage")
def usage(request: Request) -> dict[str, Any]:
    auth_context(request, store, settings)
    return store.usage_summary()


@app.post("/v1/messages")
async def anthropic_messages_proxy(request: Request) -> Response:
    # Claude Code compatibility path. Platform auth is optional in local mode.
    ctx = auth_context(request, store, settings)
    body = await request.json()
    decompute_cfg = body.get("decompute") or {}
    mode = decompute_cfg.get("yscompress", {}).get("mode", settings.yscompress_default_mode)
    if request.headers.get("x-yscompress-bypass", "false").lower() == "true":
        mode = "off"

    messages = body.get("messages") or []
    comp = await yscompress.compress_messages(messages, mode=mode, ccr=decompute_cfg.get("ccr", True))
    compressed_body = dict(body)
    compressed_body["messages"] = comp.messages

    devices = store.list_devices() or [d.to_dict() for d in scan_local_devices()]
    planner = YellowstonePlanner(scan_local_devices())
    plan = planner.plan(
        model=body.get("model", "claude"),
        traffic=TrafficProfile(comp.tokens_before, comp.tokens_after, p95_output_tokens=body.get("max_tokens", 1024)),
        compression_mode=mode,
        prompt_cache_mode=decompute_cfg.get("prompt_cache", settings.prompt_cache_mode),
    )

    upstream_key = upstream_anthropic_key(request, settings)
    prompt_cache_mode = decompute_cfg.get("prompt_cache", settings.prompt_cache_mode)
    stream = bool(body.get("stream"))

    headers = {
        "x-decompute-tokens-before": str(comp.tokens_before),
        "x-decompute-tokens-after": str(comp.tokens_after),
        "x-decompute-tokens-saved": str(comp.tokens_saved),
        "x-decompute-compression-ratio": str(comp.compression_ratio),
        "x-decompute-route": plan.route,
    }

    if stream:
        # We cannot reliably know final output tokens for proxied streams, so record input-side usage.
        store.record_usage(
            {
                "endpoint": "/v1/messages:stream",
                "model": body.get("model"),
                "raw_input_tokens": comp.tokens_before,
                "compressed_input_tokens": comp.tokens_after,
                "output_tokens": 0,
                "tokens_saved": comp.tokens_saved,
                "compression_ratio": comp.compression_ratio,
                "cache_mode": prompt_cache_mode,
                "route": plan.to_dict(),
            },
            project_id=ctx["project_id"],
            key_prefix=ctx.get("key_prefix"),
        )
        return StreamingResponse(
            stream_anthropic_messages(
                compressed_body,
                settings=settings,
                upstream_api_key=upstream_key,
                prompt_cache_mode=prompt_cache_mode,
                extra_headers=dict(request.headers),
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    result = await call_anthropic_messages(
        compressed_body,
        settings=settings,
        upstream_api_key=upstream_key,
        prompt_cache_mode=prompt_cache_mode,
        extra_headers=dict(request.headers),
    )
    output_tokens = int((result.get("usage") or {}).get("output_tokens") or estimate_tokens(result))
    store.record_usage(
        {
            "endpoint": "/v1/messages",
            "model": body.get("model"),
            "raw_input_tokens": comp.tokens_before,
            "compressed_input_tokens": comp.tokens_after,
            "output_tokens": output_tokens,
            "tokens_saved": comp.tokens_saved,
            "compression_ratio": comp.compression_ratio,
            "cache_mode": prompt_cache_mode,
            "route": plan.to_dict(),
        },
        project_id=ctx["project_id"],
        key_prefix=ctx.get("key_prefix"),
    )

    if settings.return_proxy_metadata or request.headers.get("x-decompute-metadata", "false").lower() == "true":
        result = dict(result)
        result["decompute"] = {"yscompress": comp.to_dict(), "yellowstone": plan.to_dict()}
    return JSONResponse(result, headers=headers)


@app.post("/v1/chat/completions")
async def openai_chat_proxy(request: Request) -> JSONResponse:
    ctx = auth_context(request, store, settings)
    body = await request.json()
    messages = body.get("messages") or []
    model = body.get("model", "claude-sonnet-4-5")
    mode = ((body.get("decompute") or {}).get("yscompress") or {}).get("mode", settings.yscompress_default_mode)

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

    comp = await yscompress.compress_messages(anthropic_messages, mode=mode, ccr=True)
    planner = YellowstonePlanner(scan_local_devices())
    plan = planner.plan(
        model=model,
        traffic=TrafficProfile(comp.tokens_before, comp.tokens_after, p95_output_tokens=body.get("max_tokens", 1024)),
        compression_mode=mode,
        prompt_cache_mode=settings.prompt_cache_mode,
    )
    upstream_body = {
        "model": model,
        "max_tokens": body.get("max_tokens", 1024),
        "system": "\n".join(str(s) for s in system_parts if s),
        "messages": comp.messages,
    }
    result = await call_anthropic_messages(
        upstream_body,
        settings=settings,
        upstream_api_key=upstream_anthropic_key(request, settings),
        prompt_cache_mode=settings.prompt_cache_mode,
        extra_headers=dict(request.headers),
    )
    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    output_tokens = int((result.get("usage") or {}).get("output_tokens") or estimate_tokens(text))
    store.record_usage(
        {
            "endpoint": "/v1/chat/completions",
            "model": model,
            "raw_input_tokens": comp.tokens_before,
            "compressed_input_tokens": comp.tokens_after,
            "output_tokens": output_tokens,
            "tokens_saved": comp.tokens_saved,
            "compression_ratio": comp.compression_ratio,
            "cache_mode": settings.prompt_cache_mode,
            "route": plan.to_dict(),
        },
        project_id=ctx["project_id"],
        key_prefix=ctx.get("key_prefix"),
    )
    openai_resp = {
        "id": result.get("id", "chatcmpl_mock"),
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": result.get("stop_reason", "stop")}],
        "usage": {
            "prompt_tokens": comp.tokens_after,
            "completion_tokens": output_tokens,
            "total_tokens": comp.tokens_after + output_tokens,
            "raw_prompt_tokens": comp.tokens_before,
            "tokens_saved": comp.tokens_saved,
        },
        "yellowstone": plan.to_dict(),
        "yscompress": comp.to_dict(),
    }
    return JSONResponse(openai_resp)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Decompute Gateway is running", "dashboard": "/dashboard", "docs": "/docs"}
