"""Unit tests for the streaming client-disconnect cleanup module.

Uses the project's `asyncio.run(...)` convention (no pytest-asyncio plugin).
Ported from stream_cleanup_pack and exercises the module in isolation:
output/tool/thinking accounting, completed stream, client-disconnect cancellation
(+ upstream closed + usage recorded once), generator-close, and SSE-error-after-200.
"""
import asyncio

from decompute_gateway.streaming_cleanup import (
    SSEDecoder,
    StreamUsageTracker,
    mock_anthropic_sse_stream,
    proxy_sse_stream,
)


class CloseableAsyncIter:
    """Async byte iterator that records whether it was aclose()'d — the signal that
    the upstream stream was actually cancelled."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        item = self.chunks[self.index]
        self.index += 1
        return item

    async def aclose(self):
        self.closed = True


def test_sse_decoder_tracks_output_tool_and_thinking_without_rewriting():
    raw = (
        b"event: message_start\n"
        b"data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":123,\"output_tokens\":0},\"content\":[]}}\n\n"
        b"event: ping\ndata: {\"type\":\"ping\"}\n\n"
        b"event: content_block_delta\n"
        b"data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"x\\\":\"}}\n\n"
        b"event: content_block_delta\n"
        b"data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"hmm\"}}\n\n"
        b"event: content_block_delta\n"
        b"data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"signature_delta\",\"signature\":\"sig\"}}\n\n"
        b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":89}}\n\n"
        b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
    )
    decoder = SSEDecoder()
    events = decoder.feed(raw[:100]) + decoder.feed(raw[100:])
    tracker = StreamUsageTracker()
    for ev in events:
        tracker.observe(ev)
    assert len(events) == 7
    assert tracker.input_tokens == 123
    assert tracker.output_tokens == 89
    assert tracker.message_stop_seen is True
    assert tracker.status == "completed"


def test_proxy_sse_stream_records_completed_mock_stream():
    tracker = StreamUsageTracker()
    completed = []

    async def on_complete(done):
        completed.append(done.usage_event({"raw_input_tokens": 10}))

    async def run():
        out = []
        async for chunk in proxy_sse_stream(
            mock_anthropic_sse_stream({"model": "claude-mock", "messages": [{"role": "user", "content": "hello"}]}),
            tracker=tracker, on_complete=on_complete,
        ):
            out.append(chunk)
        return b"".join(out)

    joined = asyncio.run(run())
    assert b"message_start" in joined and b"message_stop" in joined
    assert tracker.status == "completed"
    assert tracker.output_tokens > 0
    assert len(completed) == 1
    assert completed[0]["output_tokens"] == tracker.output_tokens
    assert completed[0]["stream_cancelled"] is False


def test_client_disconnect_marks_cancelled_closes_upstream_and_records_usage():
    chunks = [
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"still generating\"}}\n\n",
        b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":50}}\n\n",
    ]
    upstream = CloseableAsyncIter(chunks)
    tracker = StreamUsageTracker()
    completed = []
    checks = 0

    async def is_disconnected():
        nonlocal checks
        checks += 1
        return checks > 1  # first chunk passes; client gone on the second poll

    async def on_complete(done):
        completed.append(done.usage_event({"raw_input_tokens": 10}))

    async def run():
        out = []
        async for chunk in proxy_sse_stream(
            upstream, request_is_disconnected=is_disconnected, tracker=tracker, on_complete=on_complete
        ):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert len(out) == 1
    assert upstream.closed is True
    assert tracker.cancelled is True
    assert tracker.cancel_reason == "client_disconnected"
    assert tracker.status == "cancelled"
    assert len(completed) == 1
    assert completed[0]["stream_cancelled"] is True
    assert completed[0]["stream_cancel_reason"] == "client_disconnected"
    # The disconnect lands on the content_block_delta iteration (the terminal
    # message_delta with output_tokens=50 is never reached), so output_tokens is
    # estimated from the forwarded text — never recorded as 0.
    assert tracker.output_tokens_estimated is True
    assert tracker.output_tokens > 0
    assert completed[0]["output_tokens"] == tracker.output_tokens
    assert completed[0]["output_tokens_estimated"] is True


def test_generator_close_marks_cancelled_and_completes_once():
    upstream = CloseableAsyncIter([
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":50}}\n\n",
    ])
    tracker = StreamUsageTracker()
    completed = []

    async def on_complete(done):
        completed.append(done.to_dict())

    async def run():
        agen = proxy_sse_stream(upstream, tracker=tracker, on_complete=on_complete)
        first = await agen.__anext__()
        assert b"message_start" in first
        await agen.aclose()

    asyncio.run(run())
    assert upstream.closed is True
    assert tracker.cancelled is True
    assert tracker.cancel_reason == "stream_generator_closed"
    assert len(completed) == 1


def test_sse_error_after_200_is_recorded_as_upstream_stream_error():
    upstream = CloseableAsyncIter([
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}\n\n",
    ])
    tracker = StreamUsageTracker()

    async def run():
        async for _ in proxy_sse_stream(upstream, tracker=tracker):
            pass

    asyncio.run(run())
    assert tracker.stream_error is not None
    assert tracker.stream_error["error"]["type"] == "overloaded_error"
    assert tracker.status == "upstream_stream_error"


def test_trailing_message_delta_on_disconnect_iteration_is_counted_exactly():
    """If the chunk fetched on the disconnect-detecting iteration is the terminal
    message_delta, its cumulative output_tokens must be observed (not dropped) —
    so the recorded count is exact, not estimated."""
    chunks = [
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":42}}\n\n",
    ]
    upstream = CloseableAsyncIter(chunks)
    tracker = StreamUsageTracker()
    checks = 0

    async def is_disconnected():
        nonlocal checks
        checks += 1
        return checks > 1  # message_start forwarded; disconnect on the message_delta iteration

    async def run():
        out = []
        async for chunk in proxy_sse_stream(upstream, request_is_disconnected=is_disconnected, tracker=tracker):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert len(out) == 1                      # message_delta itself is not forwarded to the gone client
    assert tracker.cancelled is True
    assert tracker.output_tokens == 42        # ...but its usage is still accounted
    assert tracker.output_tokens_estimated is False  # exact value present -> no estimate


def test_upstream_error_http_status_is_captured_for_status_mapping():
    """The provider embeds the upstream HTTP status on the error payload; the tracker
    must surface it so accounting records the real status (e.g. 429) not a flat 502."""
    upstream = CloseableAsyncIter([
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\",\"message\":\"slow down\"},\"http_status\":429}\n\n",
    ])
    tracker = StreamUsageTracker()

    async def run():
        async for _ in proxy_sse_stream(upstream, tracker=tracker):
            pass

    asyncio.run(run())
    assert tracker.upstream_status_code == 429
    assert tracker.status == "upstream_stream_error"
    assert tracker.usage_event()["upstream_status_code"] == 429


def test_async_on_complete_cancelled_midflight_is_tracked_not_orphaned():
    """If an async on_complete is interrupted by response-task cancellation, the
    persistence task must remain referenced (tracked) and still finish — never an
    orphaned/GC-eligible task that drops the usage write."""
    from decompute_gateway import streaming_cleanup as sc

    tracker = StreamUsageTracker()
    tracker.output_tokens = 7
    persisted = []

    async def slow_on_complete(done):
        await asyncio.sleep(0.02)   # still in flight when the response task is cancelled
        persisted.append(done.output_tokens)

    async def run():
        # _safe_complete is what the proxy's finally runs; cancel it while the
        # shielded callback is mid-flight (the disconnect-cancellation scenario).
        task = asyncio.create_task(sc._safe_complete(tracker, slow_on_complete))
        await asyncio.sleep(0)       # let it create the inner task + reach the shield
        assert len(sc._PENDING_COMPLETIONS) == 1   # tracked while running
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await sc.await_pending_completions()
        return persisted

    result = asyncio.run(run())
    assert result == [7]                       # usage persistence completed despite cancellation
    assert len(sc._PENDING_COMPLETIONS) == 0    # done-callback discarded it -> no leak


def test_passthrough_preserves_event_bytes_exactly():
    """tool_use (input_json_delta), thinking_delta, signature_delta, and ping must
    reach the client byte-for-byte — the proxy yields the raw upstream chunks and
    never re-serializes them."""
    chunks = [
        b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
        b"event: ping\ndata: {\"type\":\"ping\"}\n\n",
        b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"city\\\":\"}}\n\n",
        b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"let me think\"}}\n\n",
        b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"signature_delta\",\"signature\":\"sig-abc\"}}\n\n",
        b"event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":12}}\n\n",
        b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
    ]
    upstream = CloseableAsyncIter(list(chunks))
    tracker = StreamUsageTracker()

    async def run():
        out = []
        async for c in proxy_sse_stream(upstream, tracker=tracker):
            out.append(c)
        return out

    out = asyncio.run(run())
    assert b"".join(out) == b"".join(chunks)   # byte-for-byte, every event type preserved
    assert tracker.status == "completed"
    assert tracker.output_tokens == 12         # message_delta usage still captured alongside passthrough


def test_dashboard_badge_reflects_stream_status(monkeypatch):
    """The request feed surfaces cancelled (red) and errored (amber) streams;
    a completed stream is a plain row (no badge)."""
    # Pin a valid local config before importing main (its module top runs
    # get_settings()->validate_for_startup()), so this is robust to test ordering.
    monkeypatch.setenv("DECOMPUTE_ENV", "local")
    from decompute_gateway.main import _stream_status_badge

    assert "cancelled" in _stream_status_badge({"stream_cancelled": True})
    assert "upstream_stream_error" in _stream_status_badge({"stream_status": "upstream_stream_error"})
    assert _stream_status_badge({"stream_status": "completed"}) == ""
