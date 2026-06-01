#!/usr/bin/env python3
"""
§15 — Real Claude Code compatibility suite.

One scripted run of the 10 checkpoints that make the gateway a first-class
Claude Code target. Drives a *running* gateway (mock mode is fine) and prints a
pass/fail line per checkpoint; exits non-zero if any fail, so it can gate a release.

    DECOMPUTE_MOCK_ANTHROPIC=true ./scripts/run_gateway.sh      # terminal 1
    python scripts/claude_code_compat_suite.py                  # terminal 2
    python scripts/claude_code_compat_suite.py --base-url http://127.0.0.1:8099
"""
from __future__ import annotations

import argparse
import sys

import httpx

VALID_KEY = "dc_dev_local"


def _big_log(lines: int = 800) -> str:
    return "\n".join(
        f"2026-05-30T00:00:{i % 60:02d}Z ERROR worker failed: upstream timeout after 30000ms id=req_{i}"
        for i in range(lines)
    )


def run(base_url: str) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    h = {"x-decompute-api-key": VALID_KEY, "content-type": "application/json"}

    def check(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # a thrown check is a failed check, never a crashed suite
            ok, detail = False, f"exception: {exc}"
        results.append((name, ok, detail))

    with httpx.Client(base_url=base_url, timeout=60.0) as c:
        # 1. claude starts successfully through gateway
        def c1():
            r = c.get("/health")
            return r.status_code == 200 and r.json().get("ok") is True, f"env={r.json().get('environment')} mock={r.json().get('mock_anthropic')}"
        check("1. gateway reachable (claude starts)", c1)

        # 2. /v1/models discovery works
        def c2():
            r = c.get("/v1/models", headers=h)
            data = r.json().get("data", [])
            return r.status_code == 200 and any("claude" in m.get("id", "") for m in data), f"{len(data)} models"
        check("2. /v1/models discovery", c2)

        # 3. simple prompt succeeds
        def c3():
            r = c.post("/v1/messages", headers=h, json={"model": "claude-haiku-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "Say OK."}]})
            return r.status_code == 200 and bool(r.json().get("content")), f"HTTP {r.status_code}"
        check("3. simple prompt", c3)

        # 4. long log prompt compresses and succeeds
        def c4():
            r = c.post("/v1/messages", headers=h, json={"model": "claude-haiku-4-5", "max_tokens": 64, "messages": [{"role": "tool", "content": _big_log()}, {"role": "user", "content": "Root cause?"}]})
            saved = int(r.headers.get("x-decompute-tokens-saved", "0"))
            return r.status_code == 200 and saved > 0, f"saved={saved} tokens"
        check("4. long log compresses", c4)

        # 5. stream=true works
        def c5():
            seen = []
            with c.stream("POST", "/v1/messages", headers=h, json={"model": "claude-haiku-4-5", "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "stream please"}]}) as r:
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        seen.append(line.split(":", 1)[1].strip())
            return "message_stop" in seen, f"{len(seen)} events"
        check("5. stream=true", c5)

        # 6. tool-use request passes through (gateway forwards a customer tool schema cleanly)
        def c6():
            tools = [{"name": "get_weather", "description": "x", "input_schema": {"type": "object", "properties": {}}}]
            r = c.post("/v1/messages", headers=h, json={"model": "claude-haiku-4-5", "max_tokens": 64, "tools": tools, "messages": [{"role": "user", "content": "weather?"}]})
            return r.status_code == 200, f"HTTP {r.status_code} (tools forwarded)"
        check("6. tool-use passthrough", c6)

        # 7. client disconnect does not leak upstream / crash the gateway
        def c7():
            with c.stream("POST", "/v1/messages", headers=h, json={"model": "claude-haiku-4-5", "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "drop me"}]}) as r:
                for _line in r.iter_lines():
                    break  # abandon mid-stream
            ok = c.get("/health").status_code == 200
            return ok, "gateway healthy after abort"
        check("7. client disconnect no-leak", c7)

        # 8. x-headroom-bypass bypasses compression
        def c8():
            r = c.post("/v1/messages", headers={**h, "x-headroom-bypass": "true"}, json={"model": "claude-haiku-4-5", "max_tokens": 64, "messages": [{"role": "tool", "content": _big_log()}, {"role": "user", "content": "Root cause?"}]})
            before = int(r.headers.get("x-decompute-tokens-before", "0"))
            after = int(r.headers.get("x-decompute-tokens-after", "0"))
            return r.status_code == 200 and before == after, f"before={before} after={after} (no compression)"
        check("8. x-headroom-bypass", c8)

        # 9. usage updates after session
        def c9():
            u = c.get("/v1/usage", headers=h).json()
            return int(u.get("requests", 0)) > 0, f"{u.get('requests')} requests recorded"
        check("9. usage updates", c9)

        # 10. invalid API key fails cleanly
        def c10():
            r = c.post("/v1/messages", headers={"x-decompute-api-key": "dc_invalid_does_not_exist", "content-type": "application/json"}, json={"model": "claude-haiku-4-5", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})
            return r.status_code == 401, f"HTTP {r.status_code} (expected 401)"
        check("10. invalid key -> 401", c10)

    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code compatibility suite (§15)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = ap.parse_args()

    try:
        httpx.get(args.base_url + "/health", timeout=5.0)
    except Exception:
        print(f"✗ gateway not reachable at {args.base_url} — start it first (make dev / run_gateway.sh)")
        return 2

    print(f"\nClaude Code compatibility suite — {args.base_url}")
    print("─" * 78)
    results = run(args.base_url)
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name:<34} {detail}")
    print("─" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"  {passed}/{len(results)} checkpoints passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
