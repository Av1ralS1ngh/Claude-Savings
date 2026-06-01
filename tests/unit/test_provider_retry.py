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
