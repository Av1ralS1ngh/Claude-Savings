from decompute_gateway.sse import SSEUsageTracker


def test_sse_tracker_captures_output_tokens():
    tracker = SSEUsageTracker()
    chunk = b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":12,"output_tokens":34,"cache_read_input_tokens":5}}\n\n'
    tracker.feed(chunk)
    usage = tracker.to_usage()
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 34
    assert usage["cache_read_input_tokens"] == 5
