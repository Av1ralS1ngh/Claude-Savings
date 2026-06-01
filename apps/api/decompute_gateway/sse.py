from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


@dataclass
class SSEEvent:
    event: str | None
    data: str


class SSEUsageTracker:
    """Parse Anthropic-style SSE while forwarding bytes unchanged."""

    def __init__(self) -> None:
        self.buffer = b""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.final_stop_reason: str | None = None
        self.error: dict[str, Any] | None = None
        self.events_seen: list[str] = []
        # Captured from the upstream response headers before streaming begins, so
        # streamed requests are as traceable as non-streamed ones.
        self.upstream_request_id: str | None = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n\n", 1)
            event = self._parse_event(raw.decode("utf-8", errors="ignore"))
            if event:
                self._observe(event)

    def _parse_event(self, raw: str) -> SSEEvent | None:
        event_name: str | None = None
        data_lines: list[str] = []
        for line in raw.splitlines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not event_name and not data_lines:
            return None
        return SSEEvent(event=event_name, data="\n".join(data_lines))

    def _observe(self, event: SSEEvent) -> None:
        if event.event:
            self.events_seen.append(event.event)
        if not event.data or event.data == "[DONE]":
            return
        try:
            payload = json.loads(event.data)
        except Exception:
            return
        usage = payload.get("usage") or {}
        self.input_tokens = max(self.input_tokens, int(usage.get("input_tokens") or 0))
        self.output_tokens = max(self.output_tokens, int(usage.get("output_tokens") or 0))
        self.cache_creation_input_tokens = max(self.cache_creation_input_tokens, int(usage.get("cache_creation_input_tokens") or 0))
        self.cache_read_input_tokens = max(self.cache_read_input_tokens, int(usage.get("cache_read_input_tokens") or 0))
        delta = payload.get("delta") or {}
        if delta.get("stop_reason"):
            self.final_stop_reason = delta.get("stop_reason")
        if payload.get("type") == "error" or event.event == "error":
            self.error = payload.get("error") or payload

    def to_usage(self) -> dict[str, int | str | None | dict[str, Any]]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "stop_reason": self.final_stop_reason,
            "error": self.error,
        }
