import asyncio

from decompute_gateway.anthropic_provider import stream_anthropic_messages
from decompute_gateway.config import Settings
from decompute_gateway.sse import SSEUsageTracker


def test_tool_and_thinking_deltas_are_passed_without_breaking_parser():
    tracker = SSEUsageTracker()
    tracker.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"x\\":"}}\n\n')
    tracker.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"thinking_delta","thinking":"hmm"}}\n\n')
    tracker.feed(b'event: ping\ndata: {"type":"ping"}\n\n')
    assert "content_block_delta" in tracker.events_seen
    assert "ping" in tracker.events_seen


def test_long_output_stream_accounts_final_output_tokens():
    """A long stream (many text deltas) must still report the final output token
    count from message_delta, not the running 0 from message_start."""
    tracker = SSEUsageTracker()
    tracker.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":50,"output_tokens":0}}}\n\n')
    for i in range(200):
        tracker.feed(f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"tok{i} "}}}}\n\n'.encode())
    tracker.feed(b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":312}}\n\n')
    tracker.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    u = tracker.to_usage()
    assert u["output_tokens"] == 312
    assert u["stop_reason"] == "end_turn"
    assert tracker.events_seen.count("content_block_delta") == 200


def test_mid_stream_error_after_http_200_is_captured():
    """Anthropic can emit an error event after a 200 — the tracker must surface it
    so the gateway can record the request as failed."""
    tracker = SSEUsageTracker()
    tracker.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n')
    tracker.feed(b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}\n\n')
    u = tracker.to_usage()
    assert u["error"] and u["error"]["type"] == "overloaded_error"


def test_partial_event_is_buffered_until_complete():
    """A chunk split across the SSE delimiter must not be parsed (or lost) until the
    full event arrives — the interrupted/partial-stream case."""
    tracker = SSEUsageTracker()
    tracker.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta",')  # no terminator yet
    assert tracker.events_seen == []
    tracker.feed(b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n')  # completes the event
    assert "content_block_delta" in tracker.events_seen


def test_mock_stream_captures_upstream_request_id_and_usage():
    """Driving the real streaming generator (mock path) must capture the upstream
    request-id and final output tokens — streamed requests stay traceable (§4)."""
    settings = Settings()
    tracker = SSEUsageTracker()

    async def run():
        chunks = []
        async for c in stream_anthropic_messages(
            {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]},
            settings=settings, upstream_api_key=None, prompt_cache_mode="off", tracker=tracker,
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert chunks
    assert tracker.upstream_request_id == "req_mock"
    assert "message_start" in tracker.events_seen and "message_stop" in tracker.events_seen
    assert tracker.output_tokens > 0
