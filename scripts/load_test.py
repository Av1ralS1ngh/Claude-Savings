#!/usr/bin/env python3
"""
§18 — Load & soak harness for the Decompute Gateway.

Drives a *running* gateway and reports latency percentiles + error rates against
the V0 performance targets. Designed for mock-Anthropic mode so the numbers reflect
gateway overhead (compression + routing + DB), not Anthropic round-trips.

    # terminal 1 — start the gateway in mock mode
    DECOMPUTE_MOCK_ANTHROPIC=true ./scripts/run_gateway.sh   # or: make dev

    # terminal 2 — run the load suite
    python scripts/load_test.py                       # full suite
    python scripts/load_test.py --quick               # smaller counts, fast
    python scripts/load_test.py --base-url http://127.0.0.1:8099

Scenarios (checklist §18): concurrent non-streaming, concurrent streaming,
sustained small RPM, huge-context, Headroom-unavailable (bypass), upstream-429
simulation (gateway rate limit), and client-disconnect-during-stream.

Exit code is non-zero if any hard target regresses, so this can gate a release.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# V0 performance targets (ms), excluding the Anthropic round-trip (mock mode).
TARGETS = {
    "small_p95_ms": 150.0,
    "large_p95_ms": 2000.0,
    "stream_first_byte_p95_ms": 300.0,
    "error_rate_pct": 1.0,
}

SMALL_PROMPT = {
    "model": "claude-haiku-4-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say OK."}],
}


def _huge_prompt(repeat: int = 1200) -> dict:
    log = "\n".join(
        f"2026-05-30T00:00:{i % 60:02d}Z ERROR worker timeout after 30000ms request-id=req_{i}"
        for i in range(repeat)
    )
    return {
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "messages": [
            {"role": "tool", "content": log},
            {"role": "user", "content": "What is the single root cause? One line."},
        ],
    }


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


@dataclass
class Result:
    name: str
    target_key: str | None = None
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    total: int = 0
    note: str = ""

    @property
    def error_rate(self) -> float:
        return (self.errors / self.total * 100.0) if self.total else 0.0

    def row(self) -> str:
        p50, p95, p99 = pct(self.latencies, 50), pct(self.latencies, 95), pct(self.latencies, 99)
        verdict = ""
        if self.target_key and self.latencies:
            limit = TARGETS[self.target_key]
            verdict = "PASS" if p95 <= limit else f"FAIL(>{limit:.0f})"
        return (
            f"  {self.name:<34}{self.total:>5}{self.errors:>7}{self.error_rate:>7.1f}%"
            f"{p50:>9.1f}{p95:>9.1f}{p99:>9.1f}   {verdict}{(' · ' + self.note) if self.note else ''}"
        )


async def _one(client: httpx.AsyncClient, headers: dict, body: dict) -> tuple[float, bool]:
    t0 = time.perf_counter()
    try:
        r = await client.post("/v1/messages", json=body, headers=headers, timeout=60.0)
        ok = r.status_code < 400
    except Exception:
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


async def scenario_concurrent(client, headers, *, name, target_key, body, concurrency) -> Result:
    res = Result(name=name, target_key=target_key)
    out = await asyncio.gather(*[_one(client, headers, body) for _ in range(concurrency)])
    for ms, ok in out:
        res.total += 1
        res.latencies.append(ms)
        if not ok:
            res.errors += 1
    return res


async def scenario_sustained(client, headers, *, name, body, count, rps) -> Result:
    """Fire `count` small requests paced at ~`rps` requests/second."""
    res = Result(name=name, target_key="small_p95_ms")
    interval = 1.0 / rps
    tasks = []
    for _ in range(count):
        tasks.append(asyncio.create_task(_one(client, headers, body)))
        await asyncio.sleep(interval)
    for ms, ok in await asyncio.gather(*tasks):
        res.total += 1
        res.latencies.append(ms)
        if not ok:
            res.errors += 1
    return res


async def _stream_first_byte(client, headers, body) -> tuple[float, float, bool]:
    """Returns (time_to_first_byte_ms, total_ms, ok)."""
    t0 = time.perf_counter()
    first = None
    try:
        async with client.stream("POST", "/v1/messages", json={**body, "stream": True}, headers=headers, timeout=60.0) as r:
            if r.status_code >= 400:
                return 0.0, 0.0, False
            async for _chunk in r.aiter_bytes():
                if first is None:
                    first = (time.perf_counter() - t0) * 1000.0
        return (first or 0.0), (time.perf_counter() - t0) * 1000.0, True
    except Exception:
        return 0.0, 0.0, False


async def scenario_streaming(client, headers, *, concurrency) -> Result:
    res = Result(name=f"{concurrency} concurrent streaming (TTFB)", target_key="stream_first_byte_p95_ms")
    out = await asyncio.gather(*[_stream_first_byte(client, headers, SMALL_PROMPT) for _ in range(concurrency)])
    for ttfb, _total, ok in out:
        res.total += 1
        res.latencies.append(ttfb)
        if not ok:
            res.errors += 1
    return res


async def scenario_429(client, headers, *, burst) -> Result:
    """Hammer past the per-key RPM ceiling; we EXPECT the gateway to shed load with 429.
    Success here = the limiter fires, not that every request passes."""
    res = Result(name=f"{burst} burst (rate-limit shed)")
    statuses: list[int] = []

    async def hit():
        try:
            r = await client.post("/v1/messages", json=SMALL_PROMPT, headers=headers, timeout=60.0)
            return r.status_code
        except Exception:
            return 0

    statuses = await asyncio.gather(*[hit() for _ in range(burst)])
    res.total = burst
    shed = sum(1 for s in statuses if s == 429)
    res.note = f"{shed} shed with 429, {sum(1 for s in statuses if s == 200)} ok"
    return res


async def scenario_disconnect(client, headers) -> Result:
    """Open a stream and abandon it mid-flight; the gateway must not crash and must
    stay healthy for the next request (no leaked upstream)."""
    res = Result(name="client disconnect during stream")
    res.total = 1
    try:
        async with client.stream("POST", "/v1/messages", json={**SMALL_PROMPT, "stream": True}, headers=headers, timeout=60.0) as r:
            async for _chunk in r.aiter_bytes():
                break  # drop the connection after the first event
        h = await client.get("/health", timeout=10.0)
        res.note = "gateway healthy after abort" if h.status_code == 200 else "gateway UNHEALTHY after abort"
        if h.status_code != 200:
            res.errors = 1
    except Exception as exc:
        res.errors = 1
        res.note = f"error: {exc}"
    return res


async def main() -> int:
    ap = argparse.ArgumentParser(description="Decompute Gateway load & soak harness (§18)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key", default="dc_dev_local")
    ap.add_argument("--quick", action="store_true", help="smaller counts for a fast check")
    args = ap.parse_args()

    headers = {"x-decompute-api-key": args.api_key, "content-type": "application/json"}
    bypass = {**headers, "x-headroom-bypass": "true"}
    q = args.quick

    async with httpx.AsyncClient(base_url=args.base_url) as client:
        try:
            hp = await client.get("/health", timeout=5.0)
            assert hp.status_code == 200
            mock = hp.json().get("mock_anthropic")
        except Exception:
            print(f"✗ gateway not reachable at {args.base_url} — start it first (make dev / run_gateway.sh)")
            return 2
        if not mock:
            print("⚠  gateway is NOT in mock mode — latencies will include real Anthropic round-trips; targets assume mock.")

        print(f"\nDecompute Gateway load/soak — {args.base_url}  (mock={mock}, quick={q})")
        print("─" * 104)
        print(f"  {'scenario':<34}{'reqs':>5}{'err':>7}{'err%':>8}{'p50ms':>9}{'p95ms':>9}{'p99ms':>9}   verdict")
        print("─" * 104)

        results = [
            await scenario_concurrent(client, headers, name="10 concurrent non-streaming", target_key="small_p95_ms", body=SMALL_PROMPT, concurrency=10 if q else 10),
            await scenario_streaming(client, headers, concurrency=10 if q else 25),
            await scenario_sustained(client, headers, name="sustained small (≈100/min)", body=SMALL_PROMPT, count=20 if q else 100, rps=20 if q else 30),
            await scenario_concurrent(client, headers, name="huge-context compressible", target_key="large_p95_ms", body=_huge_prompt(300 if q else 1200), concurrency=5 if q else 10),
            await scenario_concurrent(client, bypass, name="huge-context (Headroom bypass)", target_key="large_p95_ms", body=_huge_prompt(300 if q else 1200), concurrency=3 if q else 5),
            await scenario_429(client, headers, burst=40 if q else 120),
            await scenario_disconnect(client, headers),
        ]
        for r in results:
            print(r.row())
        print("─" * 104)

        # Hard gate: latency targets + global error rate on the non-shed scenarios.
        failed = []
        for r in results:
            if r.target_key and r.latencies and pct(r.latencies, 95) > TARGETS[r.target_key]:
                failed.append(f"{r.name}: p95 {pct(r.latencies,95):.0f}ms > {TARGETS[r.target_key]:.0f}ms")
            if r.target_key and r.error_rate > TARGETS["error_rate_pct"]:
                failed.append(f"{r.name}: error rate {r.error_rate:.1f}% > {TARGETS['error_rate_pct']:.0f}%")
        if failed:
            print("✗ REGRESSIONS:")
            for f in failed:
                print(f"   - {f}")
            return 1
        print("✓ all hard targets met")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
