"""FastAPI router for the command-gated teacher-student API.

Mounted from main.py via `include_teacher_student_router(app, ...)`, mirroring the
`include_support_bundle_router` factory pattern: main.py only mounts and injects existing deps
(auth, settings, the one Anthropic client). This is the ONLY sanctioned coupling between main.py and
the teacher-student feature (see tests/security/test_proxy_path_has_no_student.py).

Surfaces (plan §"Internal Gateway API Surface"):
  GET  /v1/teacher_student/health   scope routes:read
  POST /v1/teacher_student/run      scope messages:write   (non-streaming)
  POST /v1/teacher_student/local-chat  scope messages:write (direct, UNVERIFIED debug path)

Status-code rules (plan §"POST /v1/teacher_student/run"):
  feature disabled                       -> 503 {"error":"teacher_student_disabled"}
  student unavailable, teacher allowed    -> 200 route teacher_only
  student unavailable, no teacher         -> 503 {"error":"student_unavailable"}
  unsupported (tools present)              -> 200 route teacher_only (or student_unavailable)
  invalid request shape                   -> 400
  auth/scope failure                      -> existing auth error behavior
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..anthropic_provider import ProviderResult
from .schemas import LocalChatRequest, TeacherStudentRunRequest
from .service import TeacherResult, TeacherStudentService


def anthropic_text(body: dict[str, Any]) -> str:
    """Extract assistant text from an Anthropic messages response body."""
    parts: list[str] = []
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def build_teacher_student_service(
    request: Request,
    *,
    settings: Any,
    teacher_call_func: Callable[..., Any],
    upstream_auth_func: Callable[[Request], Any],
    store: Any | None = None,
    principal: Any | None = None,
) -> TeacherStudentService:
    """Construct a TeacherStudentService whose teacher_caller reuses THIS request's upstream Anthropic
    credential (the same one the /v1/messages proxy uses), so the teacher call needs no separate auth.

    Shared by the command-gated router AND the inline /v1/messages serve path so both wire the service
    identically (one teacher client, same sandbox/judge construction)."""

    async def teacher_caller(*, messages, system) -> TeacherResult:
        upstream_key = upstream_auth_func(request)
        body = {
            "model": getattr(settings, "teacher_student_teacher_model", "claude-sonnet-4-5"),
            "max_tokens": settings.local_student_max_output_tokens,
            "system": system,
            "messages": messages,
        }
        started = time.perf_counter()
        result: ProviderResult = await teacher_call_func(
            body,
            settings=settings,
            upstream_api_key=upstream_key,
            prompt_cache_mode="off",
            extra_headers=dict(request.headers),
        )
        if result.status_code >= 400:
            raise HTTPException(status_code=502, detail={"error": "teacher_upstream_error", "status": result.status_code})
        text = anthropic_text(result.body)
        usage = result.body.get("usage") or {}
        output_tokens = int(usage.get("output_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        return TeacherResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=str(result.body.get("model") or "claude-sonnet"),
            upstream_request_id=result.upstream_request_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    recorder = None
    project_id = getattr(principal, "project_id", None)
    if store is not None and project_id is not None:
        recorder = store.record_teacher_student_case
    # Sandbox (Layer 1) and judge (Layer 2) are built from config; both return None when disabled,
    # which makes the verifier apply the missing-evidence confidence caps.
    from .judge import make_judge
    from .sandbox import make_test_runner

    return TeacherStudentService(
        settings=settings,
        teacher_caller=teacher_caller,
        run_tests_fn=make_test_runner(settings),
        judge_fn=make_judge(settings, teacher_caller),
        recorder=recorder,
        project_id=project_id,
    )


def include_teacher_student_router(
    app,
    *,
    settings: Any,
    auth_func: Callable[[Request, str | None], Any],
    teacher_call_func: Callable[..., Any],
    upstream_auth_func: Callable[[Request], Any],
    store: Any | None = None,
    service_factory: Callable[..., TeacherStudentService] | None = None,
    route_prefix: str = "/v1/teacher_student",
) -> None:
    """Mount the teacher-student routes.

    `auth_func(request, scope)` -> principal (raises on failure); reuse main._auth.
    `teacher_call_func(...)` -> the gateway's call_anthropic_messages (ProviderResult).
    `upstream_auth_func(request)` -> upstream Anthropic credential for this request.
    `store` -> the gateway Store, for best-effort governed case recording (optional).
    `service_factory` is an injection seam for tests; production builds the default service.
    """
    router = APIRouter(prefix=route_prefix, tags=["teacher_student"])

    def _build_service(request: Request, *, principal: Any | None = None) -> TeacherStudentService:
        if service_factory is not None:
            return service_factory(request)
        return build_teacher_student_service(
            request,
            settings=settings,
            teacher_call_func=teacher_call_func,
            upstream_auth_func=upstream_auth_func,
            store=store,
            principal=principal,
        )

    @router.get("/health")
    async def teacher_student_health(request: Request):
        auth_func(request, "routes:read")
        service = _build_service(request)
        return JSONResponse(await service.health())

    @router.post("/run")
    async def teacher_student_run(payload: TeacherStudentRunRequest, request: Request):
        principal = auth_func(request, "messages:write")
        service = _build_service(request, principal=principal)
        try:
            resp = await service.run(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(exc)})

        if resp.route == "disabled":
            return JSONResponse(status_code=503, content={"error": "teacher_student_disabled"})
        if resp.route == "student_unavailable":
            return JSONResponse(status_code=503, content={"error": "student_unavailable"})
        return JSONResponse(content=resp.model_dump())

    @router.post("/local-chat")
    async def teacher_student_local_chat(payload: LocalChatRequest, request: Request):
        auth_func(request, "messages:write")
        if not settings.local_student_enabled and not settings.local_student_mock:
            return JSONResponse(status_code=503, content={"error": "teacher_student_disabled"})
        service = _build_service(request)
        messages = payload.normalized_messages()
        if not messages:
            raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "prompt or messages required"})
        # Reject tools at the boundary (top-level OR blocks) — the student never gets an agentic turn.
        if payload.declares_tools():
            return JSONResponse(status_code=400, content={"error": "tools_present", "message": "the local student does not accept tools"})
        try:
            resp = await service.local_chat(messages=messages, max_tokens=payload.max_tokens, temperature=payload.temperature)
        except Exception as exc:  # local provider down -> loud, no mock
            from ..local_provider import LocalProviderError

            if isinstance(exc, LocalProviderError):
                return JSONResponse(status_code=503, content={"error": getattr(exc, "reason", "student_unavailable")})
            raise
        return JSONResponse(content=resp.model_dump())

    # ── Read / admin surfaces (separate scopes; never implied by messages:write) ──

    @router.get("/summary")
    async def teacher_student_summary(request: Request):
        principal = auth_func(request, "teacher_student:read")
        if store is None:
            return JSONResponse({"cases": 0, "student_accepted": 0, "teacher_called": 0, "escalation_rate": 0.0, "by_route": {}})
        return JSONResponse(store.teacher_student_summary(project_id=principal.project_id))

    @router.get("/runs")
    async def teacher_student_runs(request: Request):
        principal = auth_func(request, "teacher_student:read")
        if store is None:
            return JSONResponse({"runs": []})
        return JSONResponse({"runs": store.list_teacher_student_runs(project_id=principal.project_id)})

    @router.get("/export/sft.jsonl")
    async def teacher_student_export_sft(request: Request):
        # Export is a DISTINCT scope — messages:write must never imply it (plan §Privacy).
        principal = auth_func(request, "distill:read")
        if store is None:
            raise HTTPException(status_code=404, detail={"error": "export_unavailable"})
        # Regulated governance stores no content -> nothing to export; 409 with a clear reason.
        mode = store.effective_capture_mode(principal.project_id)
        if mode in {"metadata_only", "regulated"}:
            return JSONResponse(status_code=409, content={
                "error": "export_forbidden",
                "reason": f"governance_mode={mode} stores no exportable content",
            })
        records, meta = store.export_distillation_records(
            project_id=principal.project_id,
            target="sft",
            allow_claude_targets=settings.teacher_student_allow_claude_training_targets,
            claude_targets_signoff=settings.teacher_student_claude_training_signoff,
        )
        # Audit the export (who, when, how many, claude-target policy).
        try:
            store.record_audit(
                project_id=principal.project_id,
                actor_key_prefix=getattr(principal, "key_prefix", None),
                action="teacher_student.export.sft",
                target_type="distillation",
                metadata=meta,
            )
        except Exception:
            pass
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        return PlainTextResponse(body, media_type="application/x-ndjson", headers={"x-decompute-export-count": str(meta["exported"])})

    app.include_router(router)
