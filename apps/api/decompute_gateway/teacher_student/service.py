"""TeacherStudentService — orchestrates student → verifier → optional Claude teacher.

Flow (plan §"Service Design"):
  1. Normalize the request into messages.
  2. Check eligibility (feature on, no tools, context within limit, coding intent ok).
  3. If ineligible: teacher-only, or disabled/unavailable per mode.
  4. Call the local student.
  5. Verify the answer (layered, escalation-biased).
  6. Accept -> return student answer; escalate -> call the existing Claude teacher path.
  7. Assemble usage/deflection metadata (persisted in Phase 3).

The teacher call is INJECTED (`teacher_caller`) so the service reuses the gateway's one Anthropic
client (`call_anthropic_messages` + `upstream_anthropic_auth`) — never a second client. The student
provider and verifier are likewise injected for testability.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import uuid
from typing import Any, Awaitable, Callable

from ..config import Settings
from ..local_provider import (
    LocalProviderError,
    LocalProviderResult,
    LocalProviderUnavailable,
    LocalProviderUnsupported,
    LocalStudentProvider,
    mock_chat,
    request_has_tools,
)
from ..pricing import cost_usd
from . import prompts, verifier
from .intent import classify_coding_intent, is_student_eligible_intent
from .schemas import (
    LocalChatResponse,
    RunUsage,
    TeacherStudentRunRequest,
    TeacherStudentRunResponse,
    Verdict,
    VerdictLayer,
)

# A teacher caller takes the normalized messages + system prompt and returns (text, input_tokens,
# output_tokens, upstream_request_id) or raises. The router supplies one bound to the request's
# auth/headers. input_tokens is what the teacher (Claude) actually billed for the prompt — needed so
# teacher-involved routes report real cost, not 0.
TeacherCaller = Callable[..., Awaitable["TeacherResult"]]


@dataclass
class TeacherResult:
    text: str
    output_tokens: int
    model: str
    upstream_request_id: str | None
    latency_ms: int
    input_tokens: int = 0


class TeacherUnavailable(Exception):
    """The teacher path is not configured/usable for this run."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def messages_to_text(messages: list[dict[str, Any]]) -> str:
    """A flat textual view of the request used for intent/verifier heuristics (not for the model)."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    parts.append(b["text"])
    return "\n".join(parts)


class TeacherStudentService:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: LocalStudentProvider | None = None,
        teacher_caller: TeacherCaller | None = None,
        run_tests_fn: verifier.TestRunner | None = None,
        judge_fn: verifier.JudgeFn | None = None,
        recorder: Callable[..., Any] | None = None,
        project_id: str | None = None,
    ):
        self.settings = settings
        self._provider = provider
        self.teacher_caller = teacher_caller
        self.run_tests_fn = run_tests_fn
        self.judge_fn = judge_fn
        # Best-effort case recorder (Phase 3). recorder(case_dict, project_id=...) -> None. Never
        # allowed to crash a run (plan invariant #9): swallowed in _record_case.
        self.recorder = recorder
        self.project_id = project_id

    # ── Provider selection ──────────────────────────────────────────────────
    # The ONLY place the in-process mock is reachable, and only via the explicit flag — never a
    # failure fallback. Production rejects the flag (config.validate_for_startup).

    @property
    def provider(self) -> LocalStudentProvider:
        # Use the process-wide singleton so the health TTL cache survives across requests (M5). An
        # explicitly-injected provider (tests) takes precedence and is never shared.
        if self._provider is None:
            self._provider = LocalStudentProvider.shared(self.settings)
        return self._provider

    async def _student_chat(self, *, messages: list[dict[str, Any]], system: str, max_tokens: int, temperature: float) -> LocalProviderResult:
        if self.settings.local_student_mock:
            return await mock_chat(model=self.settings.local_student_model, messages=messages, system=system)
        return await self.provider.chat(messages=messages, system=system, max_tokens=max_tokens, temperature=temperature)

    # ── Health ──────────────────────────────────────────────────────────────

    async def health(self, *, measure: bool = False) -> dict[str, Any]:
        s = self.settings
        # Trust tier of the configured endpoint (cheap, no I/O) — surfaced so ts-health shows whether
        # the student is loopback (trusted) / private (LAN) / public (third-party) / blocked.
        from ..endpoint_security import classify_endpoint
        tier = classify_endpoint(s.local_student_base_url).tier
        out: dict[str, Any] = {
            "enabled": s.local_student_enabled,
            "mock": s.local_student_mock,
            "student": {
                "backend": s.local_student_backend,
                "base_url": s.local_student_base_url,
                "model": s.local_student_model,
                "context_window": s.local_student_context_window,
                "trust_tier": tier,
            },
            "verifier": {
                "threshold": s.teacher_student_confidence_threshold,
                "judge_enabled": s.teacher_student_judge_enabled,
            },
            "sandbox": {
                "backend": s.teacher_student_sandbox_backend,
                "test_execution_enabled": s.teacher_student_test_execution_enabled,
            },
            "governance": {
                "capture_enabled": s.teacher_student_capture_enabled,
                "mode": s.teacher_student_governance_mode,
            },
        }
        if s.local_student_mock:
            out["student_health"] = {"ok": True, "mock": True}
        elif measure:
            # Full measured preflight (runs a tiny canary → TTFT + tokens/s). On-demand only.
            out["student_health"] = await self.provider.preflight()
        else:
            out["student_health"] = await self.provider.health()
        return out

    # ── Direct local chat (unverified debug path) ────────────────────────────

    async def local_chat(self, *, messages: list[dict[str, Any]], max_tokens: int, temperature: float) -> LocalChatResponse:
        # Refuse tools even on the debug path — the student is never handed an agentic request.
        if request_has_tools(messages=messages):
            raise LocalProviderUnsupported("tools not supported by the local student", reason="tools_present")
        res = await self._student_chat(messages=messages, system="", max_tokens=max_tokens, temperature=temperature)
        return LocalChatResponse(
            id=_new_id("tslc"),
            unverified=True,
            answer=res.text,
            model=res.model,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            latency_ms=res.latency_ms,
            request_id=res.request_id,
        )

    # ── Main verified run ────────────────────────────────────────────────────

    async def run(self, req: TeacherStudentRunRequest) -> TeacherStudentRunResponse:
        """Public entry: run the flow, then record a governed case best-effort (never crashes)."""
        resp = await self._run_inner(req)
        self._record_case(req, resp)
        return resp

    async def _run_inner(self, req: TeacherStudentRunRequest) -> TeacherStudentRunResponse:
        s = self.settings
        run_id = _new_id("tsc")

        if not s.local_student_enabled and not s.local_student_mock:
            return TeacherStudentRunResponse(id=run_id, route="disabled", final_answer=None)

        messages = req.normalized_messages()
        if not messages:
            raise ValueError("teacher_student run requires `task` or `messages`")
        task_text = messages_to_text(messages)

        # Eligibility: tools (top-level tools/tool_choice OR a tool_use/tool_result block) make the
        # student ineligible -> teacher-only. Use the request's own detector so a top-level `tools`
        # field cannot slip past (invariant #4); fall back to message-scan if the caller passed a raw
        # dict without the schema (defense in depth).
        declares_tools = req.declares_tools() if hasattr(req, "declares_tools") else request_has_tools(messages=messages)
        if declares_tools or request_has_tools(messages=messages):
            return await self._teacher_only(req, run_id, task_text, reason="tools_present")

        # If the caller demands the teacher up front, skip the student entirely.
        if req.require_teacher:
            return await self._teacher_corrected(req, run_id, task_text, student=None, verdict=None, reason="require_teacher")

        # Coding-intent eligibility (plan §"narrow coding student"): the live command path should not
        # hand the student arbitrary non-coding prose. Non-coding intent -> teacher-only.
        intent = classify_coding_intent(task_text)
        if not is_student_eligible_intent(intent):
            return await self._teacher_only(req, run_id, task_text, reason=f"ineligible_intent:{intent}")

        # Call the student.
        try:
            student = await self._student_chat(
                messages=messages,
                system=prompts.STUDENT_SYSTEM_PROMPT,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
        except LocalProviderUnavailable:
            return await self._student_unavailable_or_teacher(req, run_id, task_text)
        except LocalProviderError as exc:
            # timeout / over-context / unsupported -> escalate to teacher if possible.
            return await self._student_unavailable_or_teacher(req, run_id, task_text, reason=getattr(exc, "reason", "student_error"))

        # Verify.
        vres = await verifier.verify(
            task_text=task_text,
            answer=student.text,
            tests=req.tests,
            threshold=s.teacher_student_confidence_threshold,
            run_tests_fn=self.run_tests_fn if (req.execute_tests and s.teacher_student_test_execution_enabled) else None,
            judge_fn=self.judge_fn if s.teacher_student_judge_enabled else None,
            sandbox_backend=s.teacher_student_sandbox_backend,
        )
        verdict = _to_verdict(vres)

        if not vres.escalated:
            usage = RunUsage(
                input_tokens=student.input_tokens,
                student_output_tokens=student.output_tokens,
                student_latency_ms=student.latency_ms,
                teacher_called=False,
                estimated_teacher_cost_avoided_usd=_teacher_cost_avoided(student),
            )
            return TeacherStudentRunResponse(
                id=run_id, route="student_only",
                final_answer=student.text, student_answer=student.text,
                verdict=verdict, usage=usage, request_id=student.request_id,
            )

        # Escalate: the teacher corrects the student's attempt.
        return await self._teacher_corrected(req, run_id, task_text, student=student, verdict=verdict, reason=vres.escalation_reason)

    # ── Governed recording (best-effort; cannot crash a run) ──────────────────

    def _record_case(self, req: TeacherStudentRunRequest, resp: TeacherStudentRunResponse) -> None:
        """Persist a governed case via the injected recorder. Best-effort: any failure is swallowed,
        mirroring the gateway's _record_usage contract (plan invariant #9). The recorder itself
        enforces the storage mode, so passing content here is safe — it is dropped unless capture is
        explicitly enabled. We still hash the prompt rather than store it, so metadata-only mode keeps
        a correlator without the raw text."""
        if self.recorder is None or self.project_id is None:
            return
        if resp.route == "disabled":
            return
        try:
            task_text = messages_to_text(req.normalized_messages())
            # A plain digest of the prompt; the store HMAC-salts it before persisting (M4).
            prompt_sha = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
            verdict = resp.verdict.model_dump() if resp.verdict else {}
            request_capture = bool(req.decompute.teacher_student.capture)
            case = {
                "request_id": resp.request_id,
                "route": resp.route,
                "coding_intent": classify_coding_intent(task_text),
                "escalation_reason": verdict.get("escalation_reason"),
                "prompt_sha256": prompt_sha,
                "feature_json": {"capture_requested": request_capture},
                # Content fields — dropped by the store unless ALL THREE capture gates allow it.
                "student_answer": resp.student_answer,
                "teacher_answer": resp.teacher_answer,
                "final_answer": resp.final_answer,
                "tests_json": {"tests": req.tests} if req.tests else None,
                "input_tokens": resp.usage.input_tokens,
                "student_output_tokens": resp.usage.student_output_tokens,
                "teacher_output_tokens": resp.usage.teacher_output_tokens,
                "student_latency_ms": resp.usage.student_latency_ms,
                "teacher_latency_ms": resp.usage.teacher_latency_ms,
                "verdict": verdict,
            }
            self.recorder(case, project_id=self.project_id, request_capture=request_capture)
        except Exception:
            # Recording must never break the response (best effort, like _record_usage).
            pass

    # ── Escalation helpers ───────────────────────────────────────────────────

    async def _student_unavailable_or_teacher(self, req, run_id, task_text, *, reason: str = "student_unavailable") -> TeacherStudentRunResponse:
        """Down/error student: escalate to the teacher when allowed, else a loud student_unavailable.
        NEVER a mock answer."""
        if self._teacher_allowed():
            return await self._teacher_only(req, run_id, task_text, reason=reason)
        return TeacherStudentRunResponse(id=run_id, route="student_unavailable", final_answer=None)

    def _teacher_allowed(self) -> bool:
        return self.teacher_caller is not None

    async def _teacher_only(self, req, run_id, task_text, *, reason: str) -> TeacherStudentRunResponse:
        if not self._teacher_allowed():
            return TeacherStudentRunResponse(id=run_id, route="student_unavailable", final_answer=None)
        tr = await self._call_teacher(task_text=task_text, student_answer=None, tests=req.tests, escalation_reason=reason)
        # No student ran, so the teacher's billed input IS the run's input.
        usage = RunUsage(input_tokens=tr.input_tokens, teacher_output_tokens=tr.output_tokens, teacher_latency_ms=tr.latency_ms, teacher_called=True)
        return TeacherStudentRunResponse(
            id=run_id, route="teacher_only", final_answer=tr.text, teacher_answer=tr.text,
            usage=usage, request_id=tr.upstream_request_id,
        )

    async def _teacher_corrected(self, req, run_id, task_text, *, student, verdict, reason) -> TeacherStudentRunResponse:
        if not self._teacher_allowed():
            # Verifier wanted the teacher but none is configured: be loud, don't ship a risky answer.
            return TeacherStudentRunResponse(id=run_id, route="student_unavailable", final_answer=None, student_answer=getattr(student, "text", None), verdict=verdict)
        student_text = student.text if student is not None else None
        tr = await self._call_teacher(task_text=task_text, student_answer=student_text, tests=req.tests, escalation_reason=reason)
        usage = RunUsage(
            # Prefer the student's billed input (student + teacher see ~the same prompt); when no
            # student ran (require_teacher path), fall back to the teacher's billed input, not 0.
            input_tokens=getattr(student, "input_tokens", 0) or tr.input_tokens,
            student_output_tokens=getattr(student, "output_tokens", 0),
            student_latency_ms=getattr(student, "latency_ms", 0),
            teacher_output_tokens=tr.output_tokens,
            teacher_latency_ms=tr.latency_ms,
            teacher_called=True,
        )
        return TeacherStudentRunResponse(
            id=run_id, route="teacher_corrected", final_answer=tr.text,
            student_answer=student_text, teacher_answer=tr.text, verdict=verdict,
            usage=usage, request_id=tr.upstream_request_id,
        )

    async def _call_teacher(self, *, task_text, student_answer, tests, escalation_reason) -> TeacherResult:
        system = prompts.teacher_correction_system_prompt()
        if student_answer is None:
            user_messages = [{"role": "user", "content": task_text + (f"\n\nTests:\n{tests}" if tests else "")}]
        else:
            user_messages = prompts.build_teacher_correction_messages(
                task_text=task_text, student_answer=student_answer, escalation_reason=escalation_reason, tests=tests,
            )
        return await self.teacher_caller(messages=user_messages, system=system)


def _teacher_cost_avoided(student: LocalProviderResult) -> float:
    """A rough estimate of what a Sonnet teacher call would have cost, had the student not been
    accepted. Uses the student's token counts as a proxy for the teacher's would-be I/O."""
    return round(cost_usd(model="claude-sonnet", input_tokens=student.input_tokens, output_tokens=student.output_tokens), 6)


def _to_verdict(vres: verifier.VerifierResult) -> Verdict:
    return Verdict(
        decision=vres.decision,
        confidence=vres.confidence,
        threshold=vres.threshold,
        escalation_reason=vres.escalation_reason,
        tests_ran=vres.tests_ran,
        tests_passed=vres.tests_passed,
        sandbox_backend=vres.sandbox_backend,
        judge_model=vres.judge_model,
        layers=[VerdictLayer(name=layer.name, passed=layer.passed, detail=layer.detail, severity=layer.severity) for layer in vres.layers],
    )
