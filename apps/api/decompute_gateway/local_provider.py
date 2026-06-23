"""Local coding-student provider — an OpenAI-compatible chat-completions client.

This is the narrow, local model that answers coding tasks for the command-gated teacher-student
flow (`decompute ts-run` / `local-chat`). It speaks the OpenAI `/v1/chat/completions` surface, so
it works against llama.cpp's `llama-server`, Ollama's OpenAI route, LM Studio, vLLM, or any generic
local OpenAI-compatible server.

Design contract (see docs/teacher_student_plan.md §"Provider Design"):

  * Success returns a typed, frozen `LocalProviderResult`.
  * Every failure raises a typed exception (`LocalProviderError` subclass). No code path returns a
    canned/mock answer disguised as a real model response — a down student is a LOUD, explicit
    outcome, never a silent fallback (plan invariant #3). The only mock is an explicitly-enabled,
    clearly-flagged dev convenience (`settings.local_student_mock`), and even that is rejected in
    production by `Settings.validate_for_startup`.
  * Tool-use turns are refused up front: the student never becomes the tool-calling agent
    (plan invariant #4). Claude stays the agent.
  * All HTTP is async with finite connect/read/overall timeouts and bounded concurrency, so it can
    never block the event loop or stampede a single-GPU local server.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any

import httpx

from .config import Settings
from .yscompress import estimate_tokens


# ── Typed result ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalProviderResult:
    """A successful local-student generation. Frozen so callers can't mutate recorded usage."""

    ok: bool
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None
    raw_usage: dict[str, Any] = field(default_factory=dict)
    # True only for the explicitly-enabled dev mock. Never set by the real HTTP path, so a consumer
    # can assert it is False before trusting a result as a genuine model answer.
    mock: bool = False


# ── Typed failures (no failure ever returns a fake answer) ────────────────────


class LocalProviderError(Exception):
    """Base class for every local-student failure. Carries a stable `reason` slug for metadata."""

    reason: str = "local_provider_error"

    def __init__(self, message: str | None = None, *, reason: str | None = None):
        if reason is not None:
            self.reason = reason
        super().__init__(message or self.reason)


class LocalProviderUnavailable(LocalProviderError):
    """The student backend is unreachable, returned a non-2xx, or sent an unusable body."""

    reason = "student_unavailable"


class LocalProviderTimeout(LocalProviderError):
    """The student backend did not respond within the configured timeout budget."""

    reason = "student_timeout"


class LocalProviderOverContext(LocalProviderError):
    """The request exceeds the student's usable context window; it must not be sent."""

    reason = "over_context"


class LocalProviderUnsupported(LocalProviderError):
    """The request contains something the student is not allowed to handle (e.g. tools)."""

    reason = "unsupported"


# ── Request translation (Anthropic-ish → OpenAI chat messages) ────────────────

_TOOL_KEYS = ("tools", "tool_choice")
_TOOL_BLOCK_TYPES = {"tool_use", "tool_result"}


def _block_text(block: Any) -> str:
    """Flatten one content block to text, rejecting anything that isn't plain text."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        btype = block.get("type")
        if btype in _TOOL_BLOCK_TYPES:
            raise LocalProviderUnsupported(f"content block type {btype!r} not supported", reason="tools_present")
        if btype in (None, "text") and isinstance(block.get("text"), str):
            return block["text"]
        # input_image / document / thinking / etc. — the local coder is text-only.
        raise LocalProviderUnsupported(f"content block type {btype!r} not supported", reason="unsupported_content")
    raise LocalProviderUnsupported("unrecognized content block", reason="unsupported_content")


def _content_to_text(content: Any) -> str:
    """Convert Anthropic-style message content (string | list[block]) to a single text string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(b) for b in content if b is not None)
    return _block_text(content)


def _system_to_text(system: Any) -> str:
    """Anthropic `system` may be a string or a list of text blocks; OpenAI wants a single string."""
    return _content_to_text(system)


def to_openai_messages(*, messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, str]]:
    """Translate a request into OpenAI chat messages, refusing tool-use turns and non-text content.

    Raises `LocalProviderUnsupported` if any tool turn or unsupported block is present — the student
    is never handed an agentic/tool request (plan invariant #4)."""
    out: list[dict[str, str]] = []
    sys_text = _system_to_text(system).strip()
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for msg in messages or []:
        if not isinstance(msg, dict):
            raise LocalProviderUnsupported("message must be an object", reason="bad_request")
        role = msg.get("role", "user")
        # An assistant tool_use turn or a user tool_result turn is detected via _content_to_text,
        # which raises on those block types — so this path stays the single rejection point.
        text = _content_to_text(msg.get("content"))
        # OpenAI only knows system/user/assistant; map any tool/other role to user defensively.
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": text})
    return out


def reject_tools(body: dict[str, Any]) -> None:
    """Refuse a request whose top-level body declares tools. Mirrors the per-block rejection so the
    student is ineligible the moment tools appear anywhere (invariant #4)."""
    for key in _TOOL_KEYS:
        if body.get(key):
            raise LocalProviderUnsupported(f"request declares {key!r}", reason="tools_present")


def request_has_tools(*, messages: list[dict[str, Any]] | None = None, body: dict[str, Any] | None = None) -> bool:
    """Non-raising detector used by eligibility checks: True if the request declares tools at the top
    level OR carries any tool_use/tool_result block in its messages. Lets the service decide a route
    deterministically up front rather than relying on the provider to raise mid-call."""
    body = body or {}
    if any(body.get(k) for k in _TOOL_KEYS):
        return True
    for msg in (messages if messages is not None else body.get("messages")) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") in _TOOL_BLOCK_TYPES:
                    return True
    return False


# ── Provider ──────────────────────────────────────────────────────────────────

# One semaphore per (base_url, concurrency) so a single local GPU server isn't stampeded by parallel
# requests. Keyed by config rather than per-instance because providers are cheap, short-lived objects.
_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}

# Process-wide provider singletons keyed by (base_url, model), so the per-provider health TTL cache
# survives across requests (M5). The router builds a fresh service per request; without this the
# health probe would hit the backend every time despite health_ttl_s.
_provider_singletons: dict[tuple[str, str], "LocalStudentProvider"] = {}


def _semaphore_for(base_url: str, concurrency: int) -> asyncio.Semaphore:
    key = (base_url, max(1, concurrency))
    sem = _semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(max(1, concurrency))
        _semaphores[key] = sem
    return sem


@dataclass
class _HealthCacheEntry:
    at: float
    value: dict[str, Any]


class LocalStudentProvider:
    """OpenAI-compatible client for the local coding student.

    Construct from settings via `from_settings`. `transport` is an injection seam for tests
    (`httpx.MockTransport`); production never passes it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        context_window: int = 32_000,
        max_output_tokens: int = 2048,
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 60.0,
        overall_timeout_s: float = 120.0,
        health_ttl_s: float = 15.0,
        max_concurrency: int = 1,
        backend: str = "llama_cpp",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.overall_timeout_s = overall_timeout_s
        self.health_ttl_s = health_ttl_s
        self.max_concurrency = max_concurrency
        self.backend = backend
        self._transport = transport
        self._health_cache: _HealthCacheEntry | None = None

    @classmethod
    def from_settings(cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> "LocalStudentProvider":
        return cls(
            base_url=settings.local_student_base_url,
            model=settings.local_student_model,
            api_key=settings.local_student_api_key,
            context_window=settings.local_student_context_window,
            max_output_tokens=settings.local_student_max_output_tokens,
            connect_timeout_s=settings.local_student_connect_timeout_s,
            read_timeout_s=settings.local_student_read_timeout_s,
            overall_timeout_s=settings.local_student_overall_timeout_s,
            health_ttl_s=settings.local_student_health_ttl_s,
            max_concurrency=settings.local_student_max_concurrency,
            backend=settings.local_student_backend,
            transport=transport,
        )

    @classmethod
    def shared(cls, settings: Settings) -> "LocalStudentProvider":
        """Return a process-wide singleton keyed by (base_url, model). The router builds a fresh
        SERVICE per request, but the provider — and its health TTL cache — must persist so /health
        doesn't probe the backend on every call (M5). Test transports never use this path."""
        key = (settings.local_student_base_url, settings.local_student_model)
        inst = _provider_singletons.get(key)
        if inst is None:
            inst = cls.from_settings(settings)
            _provider_singletons[key] = inst
        return inst

    # ── HTTP plumbing ─────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key and self.api_key.lower() != "none":
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, *, read_timeout_s: float) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=read_timeout_s,
            write=self.connect_timeout_s,
            pool=self.connect_timeout_s,
        )
        kwargs: dict[str, Any] = {"timeout": timeout, "base_url": self.base_url}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _post_chat(self, payload: dict[str, Any]) -> httpx.Response:
        """POST one chat-completions request. Wrapped by wait_for in chat() for a wall-clock cap."""
        async with self._client(read_timeout_s=self.overall_timeout_s) as client:
            return await client.post("/chat/completions", json=payload, headers=self._headers())

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self, *, force: bool = False) -> dict[str, Any]:
        """Probe the backend's `/models` endpoint. Cached for `health_ttl_s` to avoid hammering a
        local server on every command. Never raises — returns a status dict either way.

        The returned dict is safe to surface via /v1/teacher_student/health: it carries no prompts,
        keys, or filesystem paths beyond the configured base URL/model."""
        now = time.monotonic()
        if not force and self._health_cache is not None and (now - self._health_cache.at) < self.health_ttl_s:
            return self._health_cache.value

        started = time.perf_counter()
        try:
            async with self._client(read_timeout_s=min(self.read_timeout_s, 8.0)) as client:
                resp = await client.get("/models", headers=self._headers())
            ok = resp.status_code < 400
            models = []
            if ok:
                body = _safe_json(resp)
                models = [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict)][:8]
            value = {
                "ok": ok,
                "backend": self.backend,
                "base_url": self.base_url,
                "model": self.model,
                "status_code": resp.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "models_loaded": models,
                "context_window": self.context_window,
            }
        except httpx.TimeoutException as exc:
            value = self._health_error("timeout", exc, started)
        except httpx.HTTPError as exc:
            value = self._health_error("unreachable", exc, started)

        self._health_cache = _HealthCacheEntry(at=now, value=value)
        return value

    def _health_error(self, kind: str, exc: Exception, started: float) -> dict[str, Any]:
        return {
            "ok": False,
            "backend": self.backend,
            "base_url": self.base_url,
            "model": self.model,
            "error": kind,
            "error_detail": type(exc).__name__,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "context_window": self.context_window,
        }

    # ── Generation ────────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: Any = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        top_p: float = 0.95,
    ) -> LocalProviderResult:
        """Generate one completion. Translates + validates the request, enforces the context window,
        and calls the backend under the concurrency semaphore. Raises a typed `LocalProviderError`
        on any failure — never returns a fabricated answer."""
        openai_messages = to_openai_messages(messages=messages, system=system)

        # Clamp the requested output budget to the server ceiling (H5): a caller cannot ask for more
        # than local_student_max_output_tokens, regardless of what the API accepted.
        requested = int(max_tokens or self.max_output_tokens)
        effective_max_tokens = max(1, min(requested, self.max_output_tokens))

        # Enforce the usable context window on prompt + OUTPUT before any network call (H5): a request
        # whose prompt fits but whose prompt+generation would overflow the window must be rejected.
        prompt_tokens_estimate = estimate_tokens(openai_messages)
        if prompt_tokens_estimate + effective_max_tokens >= self.context_window:
            raise LocalProviderOverContext(
                f"prompt ~{prompt_tokens_estimate} + max_output {effective_max_tokens} "
                f">= context window {self.context_window}"
            )

        payload = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": False,
        }

        started = time.perf_counter()
        sem = _semaphore_for(self.base_url, self.max_concurrency)
        async with sem:
            try:
                # Wall-clock cap (M1): httpx read timeout only bounds the gap between bytes, so a
                # backend that drips bytes could hold the semaphore forever. wait_for bounds the WHOLE
                # request (3.10-compatible, vs asyncio.timeout which is 3.11+), releasing the slot.
                resp = await asyncio.wait_for(
                    self._post_chat(payload), timeout=self.overall_timeout_s
                )
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                raise LocalProviderTimeout(f"student timed out: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:
                raise LocalProviderUnavailable(f"student unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            # Never echo the backend's raw body (it may include the prompt); a slug + status is enough.
            raise LocalProviderUnavailable(f"student returned HTTP {resp.status_code}")

        payload_out = _safe_json(resp)
        if not payload_out:
            raise LocalProviderUnavailable("student returned an unparseable body")
        text = extract_openai_text(payload_out)
        if not text.strip():
            raise LocalProviderUnavailable("student returned an empty completion")

        usage = payload_out.get("usage") or {}
        input_tokens = _as_int(usage.get("prompt_tokens"), prompt_tokens_estimate)
        output_tokens = _as_int(usage.get("completion_tokens"), estimate_tokens(text))
        return LocalProviderResult(
            ok=True,
            text=text,
            model=str(payload_out.get("model") or self.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=resp.headers.get("x-request-id") or resp.headers.get("request-id"),
            raw_usage=usage if isinstance(usage, dict) else {},
        )


# ── OpenAI response parsing ───────────────────────────────────────────────────


def extract_openai_text(payload: dict[str, Any]) -> str:
    """Pull assistant text out of an OpenAI chat-completions response (string or block list)."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text") or b.get("content") or "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return str(content or "")


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ── Test-only mock (explicit, flagged — NEVER a fallback) ─────────────────────


async def mock_chat(*, model: str, messages: list[dict[str, Any]], system: Any = None) -> LocalProviderResult:
    """A deterministic in-process answer for dev/tests when `settings.local_student_mock` is true.

    This is NOT a failure fallback: callers select it explicitly via the flag, the result is marked
    `mock=True`, and production rejects the flag. It exists so the teacher-student integration can be
    built and tested without a real model running."""
    # Validate the request the same way the real path does, so the mock can't paper over a tool turn.
    openai_messages = to_openai_messages(messages=messages, system=system)
    last_user = next((m["content"] for m in reversed(openai_messages) if m["role"] == "user"), "")
    text = (
        "Mock local student (DECOMPUTE_LOCAL_STUDENT_MOCK=true — not a real model).\n\n"
        "```python\n"
        "def solve():\n"
        "    raise NotImplementedError\n"
        "```"
    )
    return LocalProviderResult(
        ok=True,
        text=text,
        model=model,
        input_tokens=estimate_tokens(openai_messages),
        output_tokens=estimate_tokens(text),
        latency_ms=0,
        request_id="mock_local_student",
        raw_usage={"mock": True, "echoed_chars": len(last_user)},
        mock=True,
    )
