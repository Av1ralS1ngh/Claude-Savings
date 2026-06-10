"""§16 — upstream 5xx retry-with-backoff and 401/403 body sanitization."""
import asyncio

import pytest

from decompute_gateway import anthropic_provider as ap
from decompute_gateway.config import Settings


class _Resp:
    def __init__(self, status: int):
        self.status_code = status
        self.headers = {"request-id": "req_upstream_123"}
        self.text = "{}"

    def json(self):
        return {"ok": self.status_code}


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; returns a scripted sequence of statuses."""
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return _Resp(status)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def _fast(*a, **k):
        return None
    monkeypatch.setattr(ap.asyncio, "sleep", _fast)


def _call(monkeypatch, statuses):
    fake = _FakeAsyncClient(statuses)
    monkeypatch.setattr(ap.httpx, "AsyncClient", lambda **k: fake)
    settings = Settings(mock_anthropic=False)  # force the real upstream code path
    result = asyncio.run(ap.call_anthropic_messages(
        {"model": "claude-haiku-4-5", "messages": []},
        settings=settings, upstream_api_key="sk-test", prompt_cache_mode="off",
    ))
    return result, fake


def test_retries_transient_5xx_then_succeeds(monkeypatch):
    result, fake = _call(monkeypatch, [529, 500, 200])
    assert result.status_code == 200
    assert fake.calls == 3  # 2 retries + the success


def test_gives_up_after_max_retries(monkeypatch):
    result, fake = _call(monkeypatch, [504, 504, 504, 504])
    assert result.status_code == 504
    assert fake.calls == 3  # default upstream_max_retries=2 -> 3 attempts total


def test_does_not_retry_4xx(monkeypatch):
    result, fake = _call(monkeypatch, [400, 200])
    assert result.status_code == 400
    assert fake.calls == 1  # 400 is not retryable


def test_auth_error_body_is_sanitized_but_request_id_kept(monkeypatch):
    result, _ = _call(monkeypatch, [401])
    assert result.status_code == 401
    msg = result.body["error"]["message"]
    assert "withheld" in msg
    assert "ok" not in result.body  # raw upstream body not forwarded
    assert result.upstream_request_id == "req_upstream_123"  # still traceable


# ── Streaming path retry (mirrors the unary retry above, but BEFORE the first byte) ──

from decompute_gateway.sse import SSEUsageTracker

_OK_STREAM_CHUNKS = [
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


class _FakeStreamResp:
    def __init__(self, status, body_chunks):
        self.status_code = status
        self.headers = {"request-id": "req_upstream_strm"}
        self._body_chunks = body_chunks

    async def aread(self):
        # The error body drained on a retry, or parsed into the terminal error event.
        return b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'

    async def aiter_bytes(self):
        for c in self._body_chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeStreamClient:
    """Stands in for httpx.AsyncClient on the streaming path; .stream() returns a scripted
    status per call so we can drive 529-then-200 and persistent-529 sequences."""

    def __init__(self, statuses, body_chunks):
        self.statuses = statuses
        self.body_chunks = body_chunks
        self.stream_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        status = self.statuses[min(self.stream_calls, len(self.statuses) - 1)]
        self.stream_calls += 1
        return _FakeStreamCtx(_FakeStreamResp(status, self.body_chunks))


def _stream(monkeypatch, statuses):
    fake = _FakeStreamClient(statuses, _OK_STREAM_CHUNKS)
    monkeypatch.setattr(ap.httpx, "AsyncClient", lambda **k: fake)
    settings = Settings(mock_anthropic=False)  # force the real upstream code path
    tracker = SSEUsageTracker()

    async def run():
        out = []
        async for c in ap.stream_anthropic_messages(
            {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]},
            settings=settings, upstream_api_key="sk-test", prompt_cache_mode="off", tracker=tracker,
        ):
            out.append(c)
        return out

    return asyncio.run(run()), fake, tracker


def test_stream_retries_transient_529_then_streams_success(monkeypatch):
    """A transient 529 is absorbed at the gateway (one retry) and the client sees only a
    clean stream — never an error event it would itself retry into a storm."""
    out, fake, _ = _stream(monkeypatch, [529, 200])
    joined = b"".join(out)
    assert b"message_start" in joined and b"message_stop" in joined
    assert b"event: error" not in joined   # transient overload never surfaced to the client
    assert fake.stream_calls == 2          # one retry, then success


def test_stream_persistent_529_yields_single_terminal_error(monkeypatch):
    """Once retries are exhausted, the client gets exactly one terminal error event carrying
    the real upstream status (so it can back off) — not silence, and not a byte body."""
    out, fake, _ = _stream(monkeypatch, [529, 529, 529, 529])
    joined = b"".join(out)
    assert fake.stream_calls == 3            # default upstream_max_retries=2 -> 3 attempts
    assert joined.count(b"event: error") == 1
    assert b'"http_status":529' in joined
    assert b"message_start" not in joined    # never reached the success body
