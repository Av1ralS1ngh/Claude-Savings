#!/usr/bin/env python3
"""Decompute compression demo — one focused request, guaranteed high savings.

Sends a single /v1/messages request through the gateway where a log file IS the bulk of the
payload (the way a fresh Claude Code "read this log" turn looks, before a long session dilutes
it). It then prints the gateway's own before/after token headers and the savings %, and the
request shows up as a clean row on the dashboard. Use this in a demo instead of relying on a
long live session, where the log is a tiny slice of a 300K-token context and the headline
percentage dilutes to ~10-15% even though the log itself still compresses ~99%.

Usage:
    python scripts/demo_compress_log.py [path/to/logfile]
    GATEWAY=http://127.0.0.1:8080 python scripts/demo_compress_log.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:8080").rstrip("/")
LOGFILE = sys.argv[1] if len(sys.argv) > 1 else "/Users/avii/decompute-demo-build.log"


def device_id() -> str:
    try:
        env = json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("env", {})
        m = re.search(r"x-decompute-device:\s*(\S+)", env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "dev-demo"


def main() -> int:
    log = Path(LOGFILE).read_text(errors="ignore")
    # Shape it like a focused Claude Code turn: the log arrives as a Read tool result and is the
    # dominant content, so the dashboard % reflects the log's true compression (not a diluted session).
    body = {
        "model": "claude-haiku-4-5", "max_tokens": 64,
        "messages": [
            {"role": "user", "content": f"read {LOGFILE} and tell me which steps errored"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "I'll read it."},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": LOGFILE}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": log}]},
            {"role": "user", "content": "Which steps errored?"},
        ],
    }
    hdr = {"content-type": "application/json", "anthropic-version": "2023-06-01",
           "x-decompute-device": device_id(), "x-decompute-metadata": "true"}

    r = httpx.post(f"{GATEWAY}/v1/messages", json=body, headers=hdr, timeout=120)
    h = r.headers
    before = int(h.get("x-decompute-tokens-before", 0))
    after = int(h.get("x-decompute-tokens-after", 0))
    saved = int(h.get("x-decompute-tokens-saved", 0))
    engine = (r.json().get("decompute", {}).get("yscompress") or {}).get("engine", "?")
    pct = round((1 - after / max(before, 1)) * 100, 1)
    answer = "".join(b.get("text", "") for b in r.json().get("content", []) if isinstance(b, dict))

    print("─" * 56)
    print(f"  Log file : {LOGFILE}")
    print(f"  Sent to Claude through the Decompute gateway (HTTP {r.status_code})")
    print("─" * 56)
    print(f"  Tokens BEFORE compression : {before:>8,}")
    print(f"  Tokens AFTER  compression : {after:>8,}")
    print(f"  Tokens SAVED              : {saved:>8,}")
    print(f"  COMPRESSION SAVINGS       : {pct:>7}%   (engine: {engine})")
    print("─" * 56)
    print(f"  Claude's answer (on the COMPRESSED input):\n    {answer.strip()[:300]}")
    print("─" * 56)
    print(f"  See it on the dashboard: {GATEWAY}/dashboard  (top request row)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
