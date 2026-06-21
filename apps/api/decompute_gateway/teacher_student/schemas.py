"""Request/response schemas for the teacher-student API (plan §"POST /v1/teacher_student/run").

These are the wire contract between the command wrappers (`decompute ts-run` etc.) and the gateway.
Enum-like fields are constrained here at the edge; they are clamped AGAIN at the store boundary
(Phase 3) so a bad value can never be persisted even if a future caller bypasses these models.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# The only live request mode in V1. teacher_first / parallel are offline-eval only and are not
# accepted on the live endpoint (plan §"Mode Semantics").
RunMode = Literal["student_first"]

# Bounded provenance buckets. The wrapper sends these; the gateway stores only the bucket, never a
# raw terminal line (plan §"Command invocation metadata").
SourceBucket = Literal["cli", "vscode_command", "mcp_tool"]
CommandBucket = Literal["ts-run", "ts-health", "local-chat", "ask_local_student", "teacher_student_solve"]

# Route outcomes (plan §"Routes").
Route = Literal["student_only", "teacher_corrected", "teacher_only", "student_unavailable", "disabled"]

# Verifier decisions.
Decision = Literal["accept_student", "escalate"]


_TOOL_BLOCK_TYPES = {"tool_use", "tool_result"}


def _messages_have_tool_blocks(messages: list[dict[str, Any]] | None) -> bool:
    """True if any message carries a tool_use/tool_result content block."""
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in _TOOL_BLOCK_TYPES:
                    return True
    return False


class CaptureConfig(BaseModel):
    capture: bool = False


class DecomputeConfig(BaseModel):
    teacher_student: CaptureConfig = Field(default_factory=CaptureConfig)


class TeacherStudentRunRequest(BaseModel):
    """Body for POST /v1/teacher_student/run."""

    name: str | None = Field(default=None, max_length=200)
    task: str | None = Field(default=None, max_length=200_000)
    messages: list[dict[str, Any]] | None = None
    # Explicitly captured so a tool-bearing request is DETECTED, not silently dropped by parsing
    # (invariant #4). The student never handles tools; a request declaring them routes to teacher-only.
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    tests: str | None = Field(default=None, max_length=200_000)
    mode: RunMode = "student_first"
    require_teacher: bool = False
    # When true, run the supplied tests in the sandbox (Phase 4). Default off; honored only if the
    # server's sandbox policy permits it.
    execute_tests: bool = False
    max_tokens: int = Field(default=2048, ge=1, le=32_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    source: SourceBucket = "cli"
    command: CommandBucket = "ts-run"
    decompute: DecomputeConfig = Field(default_factory=DecomputeConfig)

    def declares_tools(self) -> bool:
        """True if the request carries tools at the top level OR in any message block (invariant #4)."""
        if self.tools or self.tool_choice:
            return True
        return _messages_have_tool_blocks(self.messages)

    def normalized_messages(self) -> list[dict[str, Any]]:
        """Return the chat messages, deriving a single user turn from `task` when `messages` is
        absent. Raises nothing — validation that there is *some* content happens in the service."""
        if self.messages:
            return self.messages
        if self.task:
            return [{"role": "user", "content": self.task}]
        return []


class VerdictLayer(BaseModel):
    name: str
    passed: bool
    detail: str | None = None
    severity: int = 0


class Verdict(BaseModel):
    decision: Decision
    confidence: float
    threshold: float
    escalation_reason: str | None = None
    layers: list[VerdictLayer] = Field(default_factory=list)


class RunUsage(BaseModel):
    input_tokens: int = 0
    student_output_tokens: int = 0
    teacher_output_tokens: int = 0
    student_latency_ms: int = 0
    teacher_latency_ms: int = 0
    teacher_called: bool = False
    estimated_teacher_cost_avoided_usd: float = 0.0


class TeacherStudentRunResponse(BaseModel):
    id: str
    route: Route
    final_answer: str | None
    student_answer: str | None = None
    teacher_answer: str | None = None
    verdict: Verdict | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    request_id: str | None = None
    usage_event_id: str | None = None


class LocalChatRequest(BaseModel):
    """Body for the direct, UNVERIFIED local-student debug path (`decompute local-chat`)."""

    prompt: str | None = Field(default=None, max_length=200_000)
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    max_tokens: int = Field(default=2048, ge=1, le=32_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    source: SourceBucket = "cli"
    command: CommandBucket = "local-chat"

    def declares_tools(self) -> bool:
        if self.tools or self.tool_choice:
            return True
        return _messages_have_tool_blocks(self.messages)

    def normalized_messages(self) -> list[dict[str, Any]]:
        if self.messages:
            return self.messages
        if self.prompt:
            return [{"role": "user", "content": self.prompt}]
        return []


class LocalChatResponse(BaseModel):
    id: str
    # Clearly labels this as the raw, UNVERIFIED local model — not a teacher-student verdict.
    unverified: bool = True
    answer: str | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    request_id: str | None = None
