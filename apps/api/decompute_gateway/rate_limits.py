from __future__ import annotations

from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import time
from typing import AsyncIterator

from fastapi import HTTPException

from .config import Settings
from .db import Store
from .security import AuthPrincipal


@dataclass
class WindowCounter:
    timestamps: deque[float]

    def add_and_count(self, now: float, window_s: float) -> int:
        self.timestamps.append(now)
        while self.timestamps and self.timestamps[0] < now - window_s:
            self.timestamps.popleft()
        return len(self.timestamps)


class SlidingTokenWindow:
    def __init__(self) -> None:
        self.items: deque[tuple[float, int]] = deque()

    def add_and_sum(self, now: float, amount: int, window_s: float) -> int:
        self.items.append((now, amount))
        while self.items and self.items[0][0] < now - window_s:
            self.items.popleft()
        return sum(v for _, v in self.items)


class RateLimitManager:
    """Single-process V0 rate limiter.

    For multi-instance production, replace with Redis while keeping this interface.
    """

    # Per-project quota overrides an admin can raise/lower at runtime. Keys match
    # the Settings field names; an absent key falls back to the global default.
    OVERRIDABLE = ("per_key_rpm", "project_tokens_per_min", "max_context_tokens", "monthly_spend_usd")

    def __init__(self, settings: Settings):
        self.settings = settings
        self.key_rpm: dict[str, WindowCounter] = defaultdict(lambda: WindowCounter(deque()))
        self.project_tokens: dict[str, SlidingTokenWindow] = defaultdict(SlidingTokenWindow)
        self.project_stream_semaphores: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(settings.max_concurrent_streams_per_project))
        # Process-local for V0 (matches the in-memory limiter); move to Redis/DB with the limiter.
        self.overrides: dict[str, dict[str, float]] = {}

    def set_override(self, project_id: str, **limits: float | None) -> dict[str, float]:
        bad = [k for k in limits if k not in self.OVERRIDABLE]
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown limit(s): {bad}. Allowed: {list(self.OVERRIDABLE)}")
        current = self.overrides.setdefault(project_id, {})
        for k, v in limits.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        if not current:
            self.overrides.pop(project_id, None)
        return self.effective_limits(project_id)

    def clear_override(self, project_id: str) -> None:
        self.overrides.pop(project_id, None)

    def effective_limits(self, project_id: str) -> dict[str, float]:
        ov = self.overrides.get(project_id, {})
        return {k: ov.get(k, getattr(self.settings, k)) for k in self.OVERRIDABLE}

    def check_body_size(self, content_length: str | None) -> None:
        if content_length:
            try:
                if int(content_length) > self.settings.max_request_bytes:
                    raise HTTPException(status_code=413, detail=f"Request too large. max_bytes={self.settings.max_request_bytes}")
            except ValueError:
                pass

    def check_preflight(self, *, principal: AuthPrincipal, estimated_input_tokens: int, estimated_output_tokens: int, store: Store) -> None:
        now = time.time()
        limits = self.effective_limits(principal.project_id)  # admin overrides win over global defaults
        key_identity = principal.key_prefix or principal.auth_mode or "anonymous"
        rpm = self.key_rpm[key_identity].add_and_count(now, 60.0)
        if rpm > limits["per_key_rpm"]:
            raise HTTPException(status_code=429, detail="Per-key request rate limit exceeded", headers={"retry-after": "60"})
        if estimated_input_tokens > limits["max_context_tokens"]:
            raise HTTPException(status_code=413, detail=f"Context too large. max_context_tokens={int(limits['max_context_tokens'])}")
        token_sum = self.project_tokens[principal.project_id].add_and_sum(now, estimated_input_tokens + estimated_output_tokens, 60.0)
        if token_sum > limits["project_tokens_per_min"]:
            raise HTTPException(status_code=429, detail="Project token-per-minute limit exceeded", headers={"retry-after": "60"})
        monthly = store.monthly_spend(project_id=principal.project_id)
        if monthly >= limits["monthly_spend_usd"]:
            raise HTTPException(status_code=402, detail="Project monthly spend cap exceeded")

    @asynccontextmanager
    async def stream_slot(self, project_id: str) -> AsyncIterator[None]:
        sem = self.project_stream_semaphores[project_id]
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=429, detail="Too many concurrent streams", headers={"retry-after": "10"})
        try:
            yield
        finally:
            sem.release()
