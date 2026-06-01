"""Concurrency-limit enforcement for streaming responses (review finding #1).

The concurrent-stream limit must surface as a real HTTP 429. A StreamingResponse
commits its 200 status line as soon as the body generator yields, so a limit
enforced *inside* the body can never become a 429 — the client just gets a 200
with an aborted stream. These tests pin the correct behaviour (acquire in the
request handler) and document the broken one (acquire inside the body) using the
real RateLimitManager and real Starlette response semantics.
"""
import fastapi
import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from decompute_gateway.config import Settings
from decompute_gateway.rate_limits import RateLimitManager


def _rl(limit: int = 1) -> RateLimitManager:
    settings = Settings()
    try:
        settings.max_concurrent_streams_per_project = limit
    except Exception:
        object.__setattr__(settings, "max_concurrent_streams_per_project", limit)
    return RateLimitManager(settings)


def test_acquire_stream_slot_enforces_limit_and_release_frees_it():
    rl = _rl(limit=1)
    release = rl.acquire_stream_slot("projX")          # slot 1 of 1
    with pytest.raises(fastapi.HTTPException) as exc:
        rl.acquire_stream_slot("projX")                # over limit
    assert exc.value.status_code == 429
    release()
    rl.acquire_stream_slot("projX")()                  # freed -> acquires, then releases
    # Idempotent release must not drive the counter negative or free a foreign slot.
    release()
    assert rl.project_stream_active["projX"] == 0


def _app_acquire_in_handler(rl: RateLimitManager) -> TestClient:
    app = fastapi.FastAPI()

    @app.post("/stream")
    async def stream():
        async def body(release):
            try:
                yield b"event: ping\ndata: {}\n\n"
            finally:
                release()

        release = rl.acquire_stream_slot("projX")      # BEFORE the response -> 429 is reachable
        return StreamingResponse(body(release), media_type="text/event-stream")

    return TestClient(app)


def _app_acquire_in_body(rl: RateLimitManager) -> TestClient:
    app = fastapi.FastAPI()

    @app.post("/stream")
    async def stream():
        async def body():
            async with rl.stream_slot("projX"):        # INSIDE the body -> too late for a 429
                yield b"event: ping\ndata: {}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    return TestClient(app)


def test_streaming_over_limit_returns_real_http_429_when_acquired_in_handler():
    rl = _rl(limit=1)
    rl.acquire_stream_slot("projX")                    # occupy the only slot (hold the release)
    client = _app_acquire_in_handler(rl)
    resp = client.post("/stream")
    assert resp.status_code == 429                     # the fix: a real HTTP rate-limit status
    assert resp.headers.get("retry-after") == "10"


def test_streaming_over_limit_leaks_as_200_when_acquired_in_body():
    """Contrast / regression guard: the old pattern (acquire inside the body) can
    only abort the stream after 200 is already committed — never a clean 429."""
    rl = _rl(limit=1)
    rl.acquire_stream_slot("projX")                    # occupy the only slot
    client = _app_acquire_in_body(rl)
    with pytest.raises(Exception):
        # The body raises HTTPException(429) after the 200 status line is sent, so
        # the server aborts the response mid-stream rather than returning a 429.
        client.post("/stream")
