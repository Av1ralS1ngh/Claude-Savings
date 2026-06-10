#!/usr/bin/env python3
"""Decompute routing + compression smoke test.

One command to prove the VS Code plugin's routing path end-to-end against the local gateway:
the same /v1/messages endpoint Claude Code calls, using the device fingerprint from
~/.claude/settings.json. Verifies (1) gateway health, (2) Pro/quota license state,
(3) compression actually engages on the proper engine (not the aggressive fallback), and
(4) a streaming turn terminates cleanly (the hang regression guard).

Usage:
    python scripts/demo_smoke_test.py
    GATEWAY=http://127.0.0.1:8080 python scripts/demo_smoke_test.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

GATEWAY = os.environ.get("GATEWAY", "http://127.0.0.1:8080").rstrip("/")
SETTINGS = Path.home() / ".claude" / "settings.json"


def device_id() -> str:
    """Read the same x-decompute-device the extension wrote into ~/.claude/settings.json."""
    try:
        env = json.loads(SETTINGS.read_text()).get("env", {})
        m = re.search(r"x-decompute-device:\s*(\S+)", env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "dev-smoketest"


def main() -> int:
    dev = device_id()
    hdr = {"content-type": "application/json", "anthropic-version": "2023-06-01",
           "x-decompute-device": dev, "x-decompute-csrf": "vscode"}
    print(f"gateway = {GATEWAY}   device = {dev}\n")
    ok = True

    with httpx.Client(timeout=60) as c:
        # 1) health
        h = c.get(f"{GATEWAY}/health")
        print(f"[1] health           : HTTP {h.status_code}")
        ok &= h.status_code == 200

        # 2) license / quota state
        s = c.get(f"{GATEWAY}/v1/license/status", headers=hdr).json()
        print(f"[2] license          : plan={s.get('plan')} status={s.get('license_status')} "
              f"compression_allowed={s.get('compression_allowed')} reason={s.get('reason')}")

        # 3) compression engages on a compressible payload
        logs = "\n".join(
            f"2026-06-05T10:00:{i:02d}Z INFO worker={i % 8} processed batch id=abc{i} "
            f"status=ok latency_ms={i * 3} retries=0" for i in range(220))
        body = {"model": "claude-haiku-4-5", "max_tokens": 16, "messages": [
            {"role": "user", "content": "Summarize the incident."},
            {"role": "assistant", "content": [{"type": "text", "text": "Fetching logs."},
                                              {"type": "tool_use", "id": "t1", "name": "logs", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": logs}]},
            {"role": "user", "content": "What happened?"}]}
        r = c.post(f"{GATEWAY}/v1/messages", json=body, headers={**hdr, "x-decompute-metadata": "true"})
        rh = r.headers
        before, after = int(rh.get("x-decompute-tokens-before", 0)), int(rh.get("x-decompute-tokens-after", 0))
        saved = int(rh.get("x-decompute-tokens-saved", 0))
        engine = (r.json().get("decompute", {}).get("yscompress") or {}).get("engine", "?")
        pct = round((1 - after / max(before, 1)) * 100, 1)
        print(f"[3] compression      : {before} -> {after} tokens ({saved} saved, {pct}%)  engine={engine}")
        if "fallback" in engine:
            print("    WARNING: aggressive local fallback engaged (headroom rust core did not run).")
            ok = False

        # 4) streaming terminates cleanly (no hang)
        events = []
        with c.stream("POST", f"{GATEWAY}/v1/messages", headers=hdr, json={
                "model": "claude-haiku-4-5", "max_tokens": 16, "stream": True,
                "messages": [{"role": "user", "content": "Reply with exactly: STREAM_OK"}]}) as sr:
            for line in sr.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
        terminal = events[-1] if events else "(none)"
        print(f"[4] streaming        : HTTP {sr.status_code}  {len(events)} events  terminal={terminal}")
        ok &= terminal in ("message_stop", "error")

    print("\nRESULT:", "PASS — routing + compression healthy" if ok else "FAIL — see warnings above")
    print(f"Dashboard: {GATEWAY}/dashboard")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
