from __future__ import annotations

"""Streaming/SSE proxy utilities for Decompute Gateway.

This module is intentionally self-contained so it can be dropped into either the
old starter gateway or the current YSCompress/Postgres branch.

It solves the production-readiness gap where /v1/messages streamed bytes from
Anthropic but recorded output_tokens=0 and did not explicitly cancel upstream
requests when the downstream Claude Code / IDE client disconnected.

Key properties:
- preserves upstream SSE bytes exactly;
- parses events only for accounting/error metadata;
- captures cumulative output_tokens from message_delta.usage.output_tokens;
- on a mid-stream cancel (before message_delta arrives) estimates output_tokens
  from the forwarded text length and flags it (output_tokens_estimated) so a
  cancelled stream is recorded with generated tokens, not 0;
- preserves ping, input_json_delta, thinking_delta, signature_delta, and unknown events;
- records stream errors that arrive after HTTP 200, capturing the upstream
  http_status the provider embeds on the error payload;
- detects downstream disconnects via request.is_disconnected();
- closes the upstream HTTP stream on disconnect/cancellation;
- runs a completion callback exactly once for usage persistence.

Known limitation: once the StreamingResponse has emitted its first byte, the HTTP
200 status line is committed, so an upstream error that surfaces after that point
(or a client cancellation) is reported to the client as an in-band SSE `error`
event rather than a non-200 status. The true status (499 cancelled / real upstream
status / 502) is still recorded in usage accounting and surfaced on the dashboard.
"""

import asyncio
import inspect
import json
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol


_log = logging.getLogger("decompute_gateway.streaming")

JsonDict = dict[str, Any]
MaybeAwaitable = Any | Awaitable[Any]

# Strong references to in-flight completion tasks spawned for async on_complete
# callbacks. Keeping them here (with a done-callback that discards) prevents an
# event-loop GC from destroying a still-running usage-persistence task if the
# response task is cancelled mid-flight — i.e. no orphaned/untracked task.
_PENDING_COMPLETIONS: set[asyncio.Task[Any]] = set()


async def await_pending_completions() -> None:
    """Await any background completion tasks — call from an app shutdown hook."""
    if _PENDING_COMPLETIONS:
        await asyncio.gather(*list(_PENDING_COMPLETIONS), return_exceptions=True)


class RequestLike(Protocol):
    async def is_disconnected(self) -> bool: ...


@dataclass(slots=True)
class SSEEvent:
    """One complete SSE event parsed from a byte stream.

    `raw` is kept for tests/debugging. The proxy still yields the original chunks
    to the downstream client; it does not re-serialize events.
    """

    event: str | None
    data: str
    raw: bytes

    def json(self) -> Any | None:
        if not self.data:
            return None
        try:
            return json.loads(self.data)
        except Exception:
            return None


class SSEDecoder:
    """Incremental, non-mutating SSE decoder.

    It supports LF and CRLF boundaries, comments, multi-line data fields, and
    unknown event types. It is intentionally tolerant because providers may add
    new event types over time.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if not chunk:
            return []

        self._buffer += chunk
        events: list[SSEEvent] = []

        while True:
            boundary = self._find_boundary(self._buffer)
            if boundary is None:
                break
            start, sep_len = boundary
            raw = self._buffer[: start + sep_len]
            self._buffer = self._buffer[start + sep_len :]
            events.append(self._parse_raw_event(raw))

        return events

    def flush(self) -> list[SSEEvent]:
        """Flush an incomplete final event.

        This should normally return nothing for well-formed SSE streams. It is
        useful for recording partial upstream errors in tests or broken streams.
        """

        if not self._buffer.strip():
            self._buffer = b""
            return []
        raw = self._buffer
        self._buffer = b""
        return [self._parse_raw_event(raw)]

    @staticmethod
    def _find_boundary(buf: bytes) -> tuple[int, int] | None:
        lf = buf.find(b"\n\n")
        crlf = buf.find(b"\r\n\r\n")

        if lf < 0 and crlf < 0:
            return None
        if lf >= 0 and (crlf < 0 or lf < crlf):
            return lf, 2
        return crlf, 4

    @staticmethod
    def _parse_raw_event(raw: bytes) -> SSEEvent:
        event_name: str | None = None
        data_lines: list[str] = []

        for raw_line in raw.splitlines():
            line = raw_line.decode("utf-8", errors="replace")
            if not line or line.startswith(":"):
                continue

            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue

            if line.startswith("data:"):
                # SSE permits a single optional leading space after the colon.
                data = line[len("data:") :]
                if data.startswith(" "):
                    data = data[1:]
                data_lines.append(data)
                continue

        return SSEEvent(event=event_name, data="\n".join(data_lines), raw=raw)


@dataclass(slots=True)
class StreamUsageTracker:
    """Mutable accounting state for one proxied stream."""

    decompute_request_id: str = field(default_factory=lambda: f"dreq_{uuid.uuid4().hex[:24]}")
    upstream_request_id: str | None = None
    upstream_status_code: int | None = None

    started_at: float = field(default_factory=time.time)
    first_byte_at: float | None = None
    finished_at: float | None = None

    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_estimated: bool = False
    output_text_chars: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    events_seen: int = 0
    last_event_type: str | None = None
    message_stop_seen: bool = False

    cancelled: bool = False
    cancel_reason: str | None = None
    stream_error: JsonDict | None = None
    gateway_exception_type: str | None = None
    gateway_exception_message: str | None = None

    completion_callback_started: bool = False
    completion_callback_ok: bool = False
    completion_callback_error: str | None = None

    def observe_raw_chunk(self) -> None:
        if self.first_byte_at is None:
            self.first_byte_at = time.time()

    def observe(self, ev: SSEEvent) -> None:
        self.events_seen += 1

        payload = ev.json()
        if isinstance(payload, dict):
            etype = str(payload.get("type") or ev.event or "unknown")
        else:
            etype = str(ev.event or "unknown")
        self.last_event_type = etype

        if not isinstance(payload, dict):
            return

        if etype == "message_start":
            message = payload.get("message") or {}
            usage = message.get("usage") or {}
            self._observe_usage(usage, include_input=True)
            return

        if etype == "message_delta":
            usage = payload.get("usage") or {}
            # Anthropic documents message_delta.usage counts as cumulative.
            self._observe_usage(usage, include_input=False)
            return

        if etype == "error":
            self.stream_error = payload
            # Providers can't carry an HTTP status inside an SSE event, so our
            # provider embeds the upstream status as `http_status` on the error
            # payload. Capture it so cancelled-vs-errored accounting can record the
            # true upstream status (429/500/529/...) rather than collapsing to 502.
            hs = payload.get("http_status")
            if isinstance(hs, int):
                self.upstream_status_code = hs
            return

        if etype == "message_stop":
            self.message_stop_seen = True
            self.mark_finished()
            return

        if etype == "content_block_delta":
            # Accumulate generated text length so a stream cancelled before the
            # terminal message_delta (which carries cumulative output_tokens) can
            # still estimate output tokens instead of recording 0. Bytes are still
            # passed through verbatim by the proxy; this is accounting only.
            delta = payload.get("delta") or {}
            if isinstance(delta, dict):
                for key in ("text", "thinking", "partial_json"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        self.output_text_chars += len(value)
            return

        # All other events, including ping, content_block_start/stop,
        # signature_delta, and future unknown event types, are pass-through only.

    def _observe_usage(self, usage: Mapping[str, Any], *, include_input: bool) -> None:
        if include_input and "input_tokens" in usage:
            self.input_tokens = _safe_int(usage.get("input_tokens"), self.input_tokens)
        if "output_tokens" in usage:
            self.output_tokens = _safe_int(usage.get("output_tokens"), self.output_tokens)
        if "cache_creation_input_tokens" in usage:
            self.cache_creation_input_tokens = _safe_int(
                usage.get("cache_creation_input_tokens"), self.cache_creation_input_tokens
            )
        if "cache_read_input_tokens" in usage:
            self.cache_read_input_tokens = _safe_int(usage.get("cache_read_input_tokens"), self.cache_read_input_tokens)

    def mark_cancelled(self, reason: str) -> None:
        self.cancelled = True
        self.cancel_reason = reason
        self.mark_finished()

    def mark_gateway_exception(self, exc: BaseException) -> None:
        self.gateway_exception_type = type(exc).__name__
        self.gateway_exception_message = str(exc)
        self.mark_finished()

    def mark_finished(self) -> None:
        if self.finished_at is not None:
            return
        self.finished_at = time.time()
        # If the stream ended before the terminal message_delta delivered the
        # cumulative output_tokens (the common case on a mid-stream client
        # disconnect), fall back to a length-based estimate of the text we actually
        # forwarded — so a cancelled stream is not billed/recorded as 0 output.
        if self.output_tokens == 0 and self.output_text_chars > 0:
            self.output_tokens = max(1, self.output_text_chars // 4)
            self.output_tokens_estimated = True

    @property
    def first_byte_ms(self) -> int | None:
        if self.first_byte_at is None:
            return None
        return int((self.first_byte_at - self.started_at) * 1000)

    @property
    def total_latency_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.gateway_exception_type:
            return "gateway_stream_error"
        if self.stream_error:
            return "upstream_stream_error"
        if self.message_stop_seen:
            return "completed"
        if self.finished_at is not None:
            return "finished_without_message_stop"
        return "incomplete"

    def to_dict(self) -> JsonDict:
        d = asdict(self)
        d["status"] = self.status
        d["first_byte_ms"] = self.first_byte_ms
        d["total_latency_ms"] = self.total_latency_ms
        return d

    def usage_event(self, base_event: Mapping[str, Any] | None = None) -> JsonDict:
        """Build a DB-ready usage event.

        This intentionally includes both common legacy fields and richer stream
        fields. Your Postgres model can ignore fields it does not yet store, or
        store the extra values in metadata_json/stream_json.
        """

        event: JsonDict = dict(base_event or {})
        event.setdefault("endpoint", "/v1/messages:stream")
        event["output_tokens"] = self.output_tokens
        event["output_tokens_estimated"] = self.output_tokens_estimated
        event["stream_status"] = self.status
        event["stream_cancelled"] = self.cancelled
        event["stream_cancel_reason"] = self.cancel_reason
        event["stream_error"] = self.stream_error
        event["stream_events_seen"] = self.events_seen
        event["stream_message_stop_seen"] = self.message_stop_seen
        event["stream_first_byte_ms"] = self.first_byte_ms
        event["stream_total_latency_ms"] = self.total_latency_ms
        event["upstream_request_id"] = self.upstream_request_id
        event["upstream_status_code"] = self.upstream_status_code
        event["decompute_request_id"] = self.decompute_request_id
        event["cache_creation_input_tokens"] = self.cache_creation_input_tokens
        event["cache_read_input_tokens"] = self.cache_read_input_tokens
        event["stream_json"] = self.to_dict()
        return event


def _error_event_bytes(message: str, http_status: int | None = None) -> bytes:
    """A terminal Anthropic-style SSE `error` event, synthesized by the gateway.

    Once a StreamingResponse has emitted its first byte the HTTP 200 is committed, so an
    upstream stall/drop/error after that point cannot change the status. Without an explicit
    terminal event the downstream client (Claude Code) keeps waiting on a silent chunked
    stream — the "stuck thinking forever" symptom. Emitting this guarantees the client's SSE
    reader always sees an end. The true status is still recorded in usage accounting.
    """
    err: JsonDict = {"type": "error", "error": {"type": "api_error", "message": f"Decompute gateway: {message}"}}
    if http_status is not None:
        err["http_status"] = http_status
    return sse_bytes("error", err)


async def proxy_sse_stream(
    upstream_chunks: AsyncIterator[bytes],
    *,
    request: RequestLike | None = None,
    request_is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
    tracker: StreamUsageTracker | None = None,
    on_complete: Callable[[StreamUsageTracker], MaybeAwaitable] | None = None,
    close_upstream: Callable[[], MaybeAwaitable] | None = None,
) -> AsyncIterator[bytes]:
    """Proxy an upstream SSE byte stream to the client with disconnect cleanup.

    Use this when you already have an upstream async byte iterator. It checks the
    downstream client before forwarding each chunk. If disconnected, it marks the
    stream as cancelled, closes the upstream iterator/context, and still runs the
    completion callback for usage persistence.
    """

    tracker = tracker or StreamUsageTracker()
    decoder = SSEDecoder()
    normal_upstream_exhausted = False

    try:
        async for chunk in upstream_chunks:
            disconnected = await _downstream_disconnected(
                request=request, request_is_disconnected=request_is_disconnected
            )

            # Account for this chunk before deciding to stop, so a trailing
            # message_delta (cumulative usage) fetched on the disconnect-detecting
            # iteration is still counted rather than silently dropped.
            if chunk:
                tracker.observe_raw_chunk()
                for ev in decoder.feed(chunk):
                    tracker.observe(ev)

            if disconnected:
                tracker.mark_cancelled("client_disconnected")
                break

            # The client is still attached: forward the raw bytes verbatim.
            if chunk:
                yield chunk
        else:
            normal_upstream_exhausted = True

        if normal_upstream_exhausted and not tracker.cancelled:
            for ev in decoder.flush():
                tracker.observe(ev)
            tracker.mark_finished()
            if not tracker.message_stop_seen and not tracker.stream_error:
                # Upstream closed cleanly but never delivered a terminal event (a
                # truncated or empty stream). The client is still attached — give it an
                # explicit end so its SSE reader doesn't wait forever.
                yield _error_event_bytes("upstream stream ended without message_stop")

    except asyncio.CancelledError:
        # Client disconnect / task cancellation: the downstream client is gone, so we must
        # NOT yield (it would error) — just record and re-raise to propagate cancellation.
        tracker.mark_cancelled("stream_task_cancelled")
        raise

    except GeneratorExit:
        tracker.mark_cancelled("stream_generator_closed")
        raise

    except Exception as exc:
        # Upstream/gateway failure after the 200 was committed (e.g. httpx ReadTimeout on a
        # stalled stream, RemoteProtocolError on a mid-stream drop). Hand the still-attached
        # client a clean terminal `error` event instead of a dangling half-written chunked
        # response, then end gracefully — re-raising here would just abort the socket with no
        # terminal, which is the hang we are fixing. The failure is recorded for accounting.
        tracker.mark_gateway_exception(exc)
        if not tracker.message_stop_seen and not tracker.stream_error:
            with suppress(Exception):
                yield _error_event_bytes(
                    f"upstream stream failed: {type(exc).__name__}", tracker.upstream_status_code
                )
        return

    finally:
        # Always close an async generator if we own it. This is what prevents a
        # disconnected Claude Code client from leaving the upstream HTTP stream
        # running until Anthropic finishes generation.
        await _safe_aclose(upstream_chunks)
        await _safe_call(close_upstream)
        await _safe_complete(tracker, on_complete)


async def mock_anthropic_sse_stream(body: Mapping[str, Any]) -> AsyncIterator[bytes]:
    """Mock stream that follows Anthropic's SSE shape for local tests."""

    text = "Mock Claude streaming response from Decompute Gateway."
    input_tokens = estimate_tokens(body.get("messages")) + estimate_tokens(body.get("system"))
    output_tokens = estimate_tokens(text)

    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock_stream",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "claude-mock"),
                    "content": [],
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]

    for event, payload in events:
        await asyncio.sleep(0)
        yield sse_bytes(event, payload)


async def record_stream_usage_to_store(
    *,
    store: Any,
    tracker: StreamUsageTracker,
    base_event: Mapping[str, Any],
    project_id: str,
    key_prefix: str | None,
) -> Any:
    """Best-effort store adapter for sync or async Store implementations."""

    event = tracker.usage_event(base_event)
    result = store.record_usage(event, project_id=project_id, key_prefix=key_prefix)
    if inspect.isawaitable(result):
        return await result
    return result


def sse_bytes(event: str, data: Mapping[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json_dumps(dict(data))}\n\n".encode("utf-8")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(value: Any) -> int:
    text = stringify(value)
    return max(1, int(len(text) / 4)) if text else 0


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


async def _downstream_disconnected(
    *, request: RequestLike | None, request_is_disconnected: Callable[[], bool | Awaitable[bool]] | None
) -> bool:
    if request_is_disconnected is not None:
        result = request_is_disconnected()
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    if request is None:
        return False

    return bool(await request.is_disconnected())


async def _safe_aclose(value: Any) -> None:
    aclose = getattr(value, "aclose", None)
    if not callable(aclose):
        return
    try:
        result = aclose()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        # Cleanup must never crash the response, but a genuine upstream-close
        # failure (e.g. a half-open httpx connection) should be visible.
        _log.warning("upstream stream aclose failed: %s: %s", type(exc).__name__, exc)


async def _safe_call(func: Callable[[], MaybeAwaitable] | None) -> None:
    if func is None:
        return
    with suppress(Exception):
        result = func()
        if inspect.isawaitable(result):
            await result


async def _safe_complete(
    tracker: StreamUsageTracker, on_complete: Callable[[StreamUsageTracker], MaybeAwaitable] | None
) -> None:
    if on_complete is None or tracker.completion_callback_started:
        return

    tracker.completion_callback_started = True
    try:
        result = on_complete(tracker)
        if inspect.isawaitable(result):
            task = asyncio.create_task(result)  # type: ignore[arg-type]
            # Hold a strong reference so a disconnect-driven cancellation of the
            # response task cannot leave this usage-persistence task orphaned and
            # GC-eligible ("Task was destroyed but it is pending!"). The
            # done-callback removes it once finished.
            _PENDING_COMPLETIONS.add(task)
            task.add_done_callback(_PENDING_COMPLETIONS.discard)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The client-disconnect cancellation should not prevent usage
                # persistence. The task keeps running, tracked above; the original
                # cancellation continues to propagate after this finally block.
                return
        tracker.completion_callback_ok = True
    except Exception as exc:
        tracker.completion_callback_error = f"{type(exc).__name__}: {exc}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default
