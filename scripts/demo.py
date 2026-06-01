"""
Decompute gateway demo — run ONE workload at a time and show the savings, then
refresh the dashboard. Uses Headroom's own benchmark data generators and the real
Claude-Code message shape (separate tool_result blocks), so the numbers are honest.

Usage:
  python scripts/demo.py --reset           # clear the dashboard to start fresh
  python scripts/demo.py code_search       # 1) GitHub code search (100 results)
  python scripts/demo.py sre                # 2) SRE incident (logs + metrics)
  python scripts/demo.py triage             # 3) GitHub issue triage (100 issues)
  python scripts/demo.py codebase           # 4) Codebase exploration
  python scripts/demo.py all                # run all four in sequence

After each run, refresh:  http://127.0.0.1:8080/dashboard
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Load the gateway's .env so you never have to `source .env` first.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

GW = "http://127.0.0.1:8080"
HEADROOM_BENCH = "/Users/avii/Desktop/Decompute_Assignment/headroom/benchmarks"
sys.path.insert(0, HEADROOM_BENCH)

from real_world_agent_benchmark import (  # noqa: E402
    generate_github_code_search, generate_log_search, generate_database_query_results,
    generate_filesystem_search, generate_github_issues,
)

# name -> (label, headroom_published_%, [data blobs], demo question)
WORKLOADS = {
    "code_search": ("Code search (100 results)", 92,
                    lambda: [generate_github_code_search("JWT middleware", 100)],
                    "Name 2 files that implement JWT middleware."),
    "sre": ("SRE incident debugging", 92,
            lambda: [generate_log_search("payment error", 300),
                     generate_database_query_results("metrics", 200),
                     generate_filesystem_search("payment", 150)],
            "What is the root cause of the 500 errors? Cite the specific error."),
    "triage": ("GitHub issue triage", 73,
               lambda: [generate_github_issues("myorg/myrepo", 100),
                        generate_github_code_search("bug fix", 80),
                        generate_log_search("exception", 200)],
               "Name the 2 highest-priority bug issues by their issue number."),
    "codebase": ("Codebase exploration", 47,
                 lambda: [generate_filesystem_search("authentication", 200),
                          generate_github_code_search("JWT auth middleware", 100)],
                 "How is authentication structured? Name the key files."),
}


def _post(path: str, body: dict):
    req = urllib.request.Request(GW + path, json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-decompute-api-key": "dc_dev_local"}, method="POST")
    with urllib.request.urlopen(req, timeout=150) as r:
        return dict(r.headers), json.loads(r.read())


def _reset():
    import psycopg
    url = os.getenv("DECOMPUTE_DATABASE_URL", "")
    if url.startswith("postgresql"):
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as c:
            c.execute("DELETE FROM usage_events")
            c.commit()
        print("✓ Dashboard reset (Postgres). Open http://127.0.0.1:8080/dashboard\n")
    else:
        print("DECOMPUTE_DATABASE_URL not set to Postgres; run with the gateway's .env sourced.\n")


def _run(key: str):
    label, headroom_pct, make_blobs, question = WORKLOADS[key]
    blobs = make_blobs()
    # Real Claude-Code shape: each tool output is its own tool_result block.
    msgs = [{"role": "user", "content": "Gather the data I need."}]
    for i, b in enumerate(blobs):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"Running tool {i+1}."},
                                                      {"type": "tool_use", "id": f"t{i}", "name": "tool", "input": {}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": json.dumps(b)}]})
    msgs.append({"role": "user", "content": question})

    print("─" * 66)
    print(f"  {label}")
    print("─" * 66)
    try:
        hdrs, resp = _post("/v1/messages", {"model": "claude-haiku-4-5", "max_tokens": 200, "messages": msgs})
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:160]}")
        return
    before = int(hdrs.get("x-decompute-tokens-before", 0))
    after = int(hdrs.get("x-decompute-tokens-after", 0))
    saved = max(0, before - after)
    pct = saved / before * 100 if before else 0
    rounds = hdrs.get("x-decompute-ccr-retrieve-rounds", "0")
    answer = next((b["text"] for b in resp.get("content", []) if b.get("type") == "text"), "")

    print(f"  Question     : {question}")
    print(f"  Raw tokens   : {before:,}")
    print(f"  Sent tokens  : {after:,}")
    print(f"  TOKENS SAVED : {saved:,}   ({pct:.1f}%)")
    print(f"  Headroom ref : {headroom_pct}%   (their published number)")
    print(f"  Retrieved    : {rounds} round(s)   (0 = answered from compressed data)")
    print(f"\n  Claude's answer (proof the data survived):")
    import textwrap
    for line in textwrap.wrap(answer.strip().replace("\n", " "), width=60)[:6]:
        print(f"    {line}")
    print(f"\n  → Refresh the dashboard: {GW}/dashboard\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workload", nargs="?", choices=list(WORKLOADS) + ["all"])
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    if args.reset:
        _reset()
    if args.workload == "all":
        for k in WORKLOADS:
            _run(k)
    elif args.workload:
        _run(args.workload)
    elif not args.reset:
        ap.print_help()


if __name__ == "__main__":
    main()
