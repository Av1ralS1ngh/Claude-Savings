"""
Replicate Headroom's published "Proof" benchmark workloads through OUR gateway.

Imports Headroom's *actual* MCP-output data generators from their open-source
benchmark (benchmarks/real_world_agent_benchmark.py), builds the same four
workloads they advertise (Code search, SRE incident, GitHub issue triage,
Codebase exploration), converts each to valid Anthropic format, and sends them
through the live gateway so the savings show up in the dashboard.

This is a faithful replication: same data generators, same scenario composition.
The only change is OpenAI tool format → Anthropic tool_result blocks, because our
/v1/messages path targets the Anthropic Messages API.

Usage:
  python scripts/replicate_headroom_benchmark.py --reset
Requires the gateway on 127.0.0.1:8080 and the headroom repo on disk.
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

# Import Headroom's real benchmark generators.
HEADROOM_BENCH = Path("/Users/avii/Desktop/Decompute_Assignment/headroom/benchmarks")
if not HEADROOM_BENCH.exists():
    sys.exit(f"Headroom benchmark dir not found: {HEADROOM_BENCH}")
sys.path.insert(0, str(HEADROOM_BENCH))

from real_world_agent_benchmark import (  # noqa: E402
    generate_github_code_search,
    create_sre_debugging_scenario,
    create_codebase_exploration_scenario,
    create_issue_triage_scenario,
)


def scenario_to_anthropic(user_query: str, tool_outputs: list[dict], final_q: str) -> list[dict]:
    """Convert a Headroom AgentScenario (OpenAI-shaped tool outputs) into valid
    Anthropic messages with tool_use / tool_result blocks."""
    msgs: list[dict] = [{"role": "user", "content": user_query}]
    for i, out in enumerate(tool_outputs):
        tid = f"tool_{i}"
        tool_name = out.get("tool", f"tool_{i}") if isinstance(out, dict) else f"tool_{i}"
        result = out.get("result", out) if isinstance(out, dict) else out
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"Calling {tool_name}."},
            {"type": "tool_use", "id": tid, "name": tool_name, "input": {}},
        ]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": json.dumps(result) if not isinstance(result, str) else result},
        ]})
    msgs.append({"role": "user", "content": final_q})
    return msgs


def build_workloads() -> list[tuple[str, list[dict]]]:
    # 1. Code search (100 results) — standalone, matches their headline row.
    code_search = generate_github_code_search("JWT authentication middleware", num_results=100)
    w_code = scenario_to_anthropic(
        "Search the codebase for JWT authentication middleware.",
        [code_search],
        "Which results actually implement the middleware?",
    )

    # 2–4. The three composed scenarios, using their exact builders.
    sre = create_sre_debugging_scenario()
    explore = create_codebase_exploration_scenario()
    triage = create_issue_triage_scenario()

    return [
        ("code_search_100", w_code),
        ("sre_incident", scenario_to_anthropic(sre.user_query, sre.tools, "What is the root cause?")),
        ("issue_triage", scenario_to_anthropic(triage.user_query, triage.tools, "Top 3 priorities?")),
        ("codebase_exploration", scenario_to_anthropic(explore.user_query, explore.tools, "Explain the auth flow.")),
    ]


def post(path: str, body: dict):
    req = urllib.request.Request(GW + path, json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-decompute-api-key": KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), {"error": e.read().decode()[:200]}


def get(path: str) -> dict:
    req = urllib.request.Request(GW + path, headers={"x-decompute-api-key": KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    if args.reset:
        # Clear the DB the gateway actually uses (Postgres when configured), not
        # only the local SQLite file.
        import os
        url = os.getenv("DECOMPUTE_DATABASE_URL", "")
        if url.startswith("postgresql"):
            import psycopg
            with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
                n = conn.execute("DELETE FROM usage_events").rowcount
                conn.commit()
            print(f"Dashboard reset (Postgres) — cleared {n} events\n")
        else:
            conn = sqlite3.connect(DB)
            n = conn.execute("DELETE FROM usage_events").rowcount
            conn.commit(); conn.close()
            print(f"Dashboard reset (SQLite) — cleared {n} events\n")

    print("─" * 78)
    print(f"  {'workload':<24}{'before':>9}{'after':>9}{'saved':>9}   route")
    print("─" * 78)
    for name, msgs in build_workloads():
        status, hdrs, body = post("/v1/messages", {"model": "claude-haiku-4-5", "max_tokens": 80, "messages": msgs})
        if status != 200:
            print(f"  {name:<24}  ERROR {status}: {body.get('error', body)}")
            continue
        before = int(hdrs.get("x-decompute-tokens-before", 0))
        after = int(hdrs.get("x-decompute-tokens-after", 0))
        pct = (1 - after / max(before, 1)) * 100
        print(f"  {name:<24}{before:>9,}{after:>9,}{pct:>8.1f}%   {hdrs.get('x-decompute-route', '?')}")
    print("─" * 78)

    u = get("/v1/usage")
    print(f"\n  DASHBOARD ({GW}/dashboard)")
    print(f"    requests      : {u['requests']}")
    print(f"    raw tokens    : {u['raw_input_tokens']:,}")
    print(f"    sent tokens   : {u['sent_input_tokens']:,}")
    print(f"    total savings : {u['estimated_savings_percent']:.1f}%")
    print(f"    cost / saved  : ${u['estimated_cost_usd']:.4f} / ${u['estimated_savings_usd']:.4f}\n")


if __name__ == "__main__":
    main()
