"""
Send the real-world session fixtures through the LIVE gateway so they show up
in the dashboard (/v1/usage). Reuses tests/fixtures/real_world_sessions.py so the
benchmark and the dashboard demo never drift apart.

Usage:
  python scripts/run_realworld_through_gateway.py            # send all scenarios
  python scripts/run_realworld_through_gateway.py --reset    # clear dashboard first

Requires the gateway running on 127.0.0.1:8080 (mock or real Anthropic both work).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
import urllib.error
from pathlib import Path

GW = "http://127.0.0.1:8080"
KEY = "dc_dev_local"
DB = Path(__file__).resolve().parents[1] / ".decompute" / "decompute.db"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "fixtures"))
from real_world_sessions import SCENARIOS  # noqa: E402


def post(path: str, body: dict) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        GW + path, json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-decompute-api-key": KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), {"error": e.read().decode()[:200]}


def get(path: str) -> dict:
    req = urllib.request.Request(GW + path, headers={"x-decompute-api-key": KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def reset_dashboard() -> None:
    """Clear usage_events from whatever DB the gateway actually uses.
    Honors DECOMPUTE_DATABASE_URL so it works on Postgres, not just SQLite."""
    import os
    url = os.getenv("DECOMPUTE_DATABASE_URL", "")
    if url.startswith("postgresql"):
        import psycopg
        dsn = url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as conn:
            n = conn.execute("DELETE FROM usage_events").rowcount
            conn.commit()
        print(f"Dashboard reset (Postgres) — cleared {n} events\n")
    else:
        conn = sqlite3.connect(DB)
        n = conn.execute("DELETE FROM usage_events").rowcount
        conn.commit()
        conn.close()
        print(f"Dashboard reset (SQLite) — cleared {n} events\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="clear usage_events first")
    args = ap.parse_args()

    if args.reset:
        reset_dashboard()

    print("─" * 74)
    print(f"  {'scenario':<22}{'before':>8}{'after':>8}{'saved':>9}   route")
    print("─" * 74)
    for s in SCENARIOS:
        status, hdrs, body = post("/v1/messages", {
            "model": "claude-haiku-4-5", "max_tokens": 80, "messages": s["messages"],
        })
        if status != 200:
            print(f"  {s['name']:<22}  ERROR {status}: {body.get('error', body)}")
            continue
        before = int(hdrs.get("x-decompute-tokens-before", 0))
        after = int(hdrs.get("x-decompute-tokens-after", 0))
        pct = (1 - after / max(before, 1)) * 100
        route = hdrs.get("x-decompute-route", "?")
        print(f"  {s['name']:<22}{before:>8,}{after:>8,}{pct:>8.1f}%   {route}")
    print("─" * 74)

    u = get("/v1/usage")
    print(f"\n  DASHBOARD ({GW}/dashboard)")
    print(f"    requests        : {u['requests']}")
    print(f"    raw tokens      : {u['raw_input_tokens']:,}")
    print(f"    sent tokens     : {u['sent_input_tokens']:,}")
    print(f"    total savings   : {u['estimated_savings_percent']:.1f}%")
    print(f"    estimated cost  : ${u['estimated_cost_usd']:.4f}")
    print(f"    estimated saved : ${u['estimated_savings_usd']:.4f}\n")


if __name__ == "__main__":
    main()
