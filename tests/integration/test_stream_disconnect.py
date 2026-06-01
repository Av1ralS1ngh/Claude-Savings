"""Gateway-level streaming disconnect tests.

Exercise our real provider (mock path) wrapped by proxy_sse_stream exactly as the
/v1/messages stream branch wires it: completed stream records usage; a downstream
disconnect cancels the upstream, records stream_cancelled=true, and runs the
completion callback once; the concurrent-stream semaphore is released on exit.
"""
import asyncio
import os

import fastapi

from decompute_gateway.anthropic_provider import stream_anthropic_messages
from decompute_gateway.config import Settings
from decompute_gateway.sse import SSEUsageTracker
from decompute_gateway.streaming_cleanup import StreamUsageTracker, proxy_sse_stream


def _mock_upstream(legacy):
    # mock path (upstream_api_key=None) — no network, deterministic SSE
    return stream_anthropic_messages(
        {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]},
        settings=Settings(), upstream_api_key=None, prompt_cache_mode="off", tracker=legacy,
    )


def test_mock_stream_completes_and_records_once():
    legacy = SSEUsageTracker()
    st = StreamUsageTracker()
    recorded = []

    async def on_complete(t):
        t.upstream_request_id = t.upstream_request_id or legacy.upstream_request_id  # route copies this
        recorded.append(t.usage_event({"endpoint": "/v1/messages:stream"}))

    async def run():
        out = []
        async for c in proxy_sse_stream(_mock_upstream(legacy), tracker=st, on_complete=on_complete):
            out.append(c)
        return out

    out = asyncio.run(run())
    assert out and st.status == "completed" and st.cancelled is False
    assert st.output_tokens > 0
    assert len(recorded) == 1
    assert recorded[0]["stream_status"] == "completed"
    assert recorded[0]["stream_cancelled"] is False
    assert recorded[0]["upstream_request_id"] == "req_mock"  # captured via legacy tracker


def test_client_disconnect_cancels_upstream_and_records_cancelled():
    legacy = SSEUsageTracker()
    st = StreamUsageTracker()
    recorded = []

    class FakeRequest:
        def __init__(self):
            self.n = 0

        async def is_disconnected(self):
            self.n += 1
            return self.n > 1  # first chunk forwarded; client gone on the next poll

    async def on_complete(t):
        recorded.append({"cancelled": t.cancelled, "reason": t.cancel_reason, "status": t.status})

    async def run():
        out = []
        async for c in proxy_sse_stream(_mock_upstream(legacy), request=FakeRequest(), tracker=st, on_complete=on_complete):
            out.append(c)
        return out

    out = asyncio.run(run())
    assert len(out) == 1                                   # stopped right after the first chunk
    assert st.cancelled is True
    assert st.cancel_reason == "client_disconnected"
    assert st.status == "cancelled"
    assert len(recorded) == 1 and recorded[0]["cancelled"] is True
    assert recorded[0]["status"] == "cancelled"


def test_disconnect_after_text_estimates_output_tokens_not_zero():
    """A mid-stream disconnect after text was forwarded but before the terminal
    message_delta must record an estimated output_tokens, never 0 (review #2/#5/#8)."""
    legacy = SSEUsageTracker()
    st = StreamUsageTracker()
    recorded = []

    class FakeRequest:
        def __init__(self):
            self.n = 0

        async def is_disconnected(self):
            self.n += 1
            # message_start, content_block_start, content_block_delta(text) forwarded;
            # disconnect on the 4th poll, before message_delta carries cumulative usage.
            return self.n > 3

    async def on_complete(t):
        recorded.append(t.usage_event({"endpoint": "/v1/messages:stream"}))

    async def run():
        async for _ in proxy_sse_stream(_mock_upstream(legacy), request=FakeRequest(), tracker=st, on_complete=on_complete):
            pass

    asyncio.run(run())
    assert st.cancelled is True
    assert st.output_tokens > 0                 # estimated from forwarded text, not 0
    assert st.output_tokens_estimated is True
    assert recorded[0]["output_tokens"] == st.output_tokens
    assert recorded[0]["output_tokens_estimated"] is True


def test_stream_slot_semaphore_decrements_on_exit():
    """The concurrent-stream counter must be released on exit so a follow-up stream
    gets a slot — covers the 'decrement concurrent stream counter' requirement."""
    from decompute_gateway.rate_limits import RateLimitManager

    settings = Settings()
    try:
        settings.max_concurrent_streams_per_project = 1
    except Exception:
        object.__setattr__(settings, "max_concurrent_streams_per_project", 1)
    rl = RateLimitManager(settings)

    async def run():
        async with rl.stream_slot("projX"):
            over_limit = False
            try:
                async with rl.stream_slot("projX"):
                    pass
            except fastapi.HTTPException as exc:
                over_limit = exc.status_code == 429
            assert over_limit, "second concurrent stream should hit the 429 limit"
        # first slot released -> a new stream acquires successfully
        async with rl.stream_slot("projX"):
            pass

    asyncio.run(run())
