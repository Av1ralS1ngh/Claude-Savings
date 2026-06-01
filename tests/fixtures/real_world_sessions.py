"""
Realistic agent-session fixtures in valid Anthropic Messages format.

These mirror how real Claude Code / coding-agent traffic actually looks:
  - user instructions (must be preserved — never compressed)
  - assistant turns with `tool_use` blocks (Claude calling Read/Grep/Bash/...)
  - user turns with `tool_result` blocks carrying the bulky output
    (file contents, search results, command logs, JSON API responses)

Used by tests/test_compression_realworld.py as a reusable regression +
optimization harness. Add new scenarios here as new workloads appear.

Every scenario is a dict:
  {
    "name": str,
    "description": str,
    "expect_compressible": bool,   # True if tool output dominates → should shrink
    "min_savings": float,          # regression floor (0.0–1.0); tune-up should raise it
    "messages": [ ...valid Anthropic messages... ],
  }

`min_savings` is a regression floor recorded from observed Rust-core behaviour,
set conservatively below the current number. If a compressor change drops a
workload below its floor, the test fails — that's the regression catch. If you
*improve* the compressor, raise the floor so the gain is locked in.
"""
from __future__ import annotations

import json


def _tool_use(text: str, tool_id: str, name: str, inp: dict) -> dict:
    return {"role": "assistant", "content": [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": tool_id, "name": name, "input": inp},
    ]}


def _tool_result(tool_id: str, content: str) -> dict:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content},
    ]}


# ── Scenario 1: debugging session — grep + read file + run tests ──────────────
def _debugging_session() -> list[dict]:
    grep_json = json.dumps([
        {"file": f"src/auth/handler_{i}.py", "line": i * 9,
         "text": f"def verify_token_{i}(token: str, *, strict: bool = True) -> Claims:",
         "match": "verify_token", "score": round(0.97 - i * 0.002, 4)}
        for i in range(90)
    ], indent=2)

    file_read = "\n".join(
        ["import jwt", "from dataclasses import dataclass", "from .errors import AuthError", ""]
        + [f"@dataclass\nclass Claims_{i}:\n    sub: str\n    exp: int\n    scopes: list[str]\n"
           for i in range(30)]
        + ["def verify_token(token, strict=True):",
           "    try:",
           "        payload = jwt.decode(token, KEY, algorithms=['HS256'])",
           "    except jwt.ExpiredSignatureError:",
           "        raise AuthError('token expired')",
           "    return Claims(**payload)"]
    )

    test_log = "\n".join(
        [f"tests/test_auth.py::test_verify_token_{i % 40} PASSED" for i in range(200)]
        + ["tests/test_auth.py::test_verify_token_expired FAILED",
           "E   AuthError: token expired",
           "E   assert 401 == 200",
           "=== 1 failed, 200 passed in 4.21s ==="]
    )

    return [
        {"role": "user", "content": "The auth tests are failing on expired tokens. Find the verify_token function, read its file, and run the auth tests."},
        _tool_use("I'll search for verify_token.", "t1", "grep", {"pattern": "verify_token"}),
        _tool_result("t1", grep_json),
        _tool_use("Let me read the handler.", "t2", "read_file", {"path": "src/auth/handler.py"}),
        _tool_result("t2", file_read),
        _tool_use("Running the auth tests.", "t3", "bash", {"cmd": "pytest tests/test_auth.py"}),
        _tool_result("t3", test_log),
        {"role": "user", "content": "Given all that, what's the root cause of the expired-token failure and the minimal fix?"},
    ]


# ── Scenario 2: SRE incident — big repetitive logs (log dedup target) ─────────
def _sre_incident() -> list[dict]:
    logs = "\n".join(
        [f"2026-05-30T0{i//3600}:{(i%3600)//60:02d}:{i%60:02d}Z INFO  pod=api-{i%8} GET /healthz 200 1ms" for i in range(500)]
        + [f"2026-05-30T01:{i:02d}:00Z ERROR pod=api-3 upstream timeout connecting to db-primary:5432" for i in range(30)]
        + ["2026-05-30T01:31:00Z FATAL pod=api-3 connection pool exhausted (50/50)",
           "2026-05-30T01:31:01Z ERROR pod=api-3 returning 503 to 1422 queued requests"]
    )
    metrics = json.dumps({
        "pods": [{"name": f"api-{i}", "cpu": f"{40 + i*3}%", "mem": f"{55 + i*2}%",
                  "restarts": i % 4, "status": "Running" if i != 3 else "CrashLoopBackOff"}
                 for i in range(8)],
        "db": {"connections": 50, "max_connections": 50, "slow_queries_last_5m": 312},
    }, indent=2)
    return [
        {"role": "user", "content": "Production is throwing 503s. Pull the api pod logs and the cluster metrics and tell me what's happening."},
        _tool_use("Fetching logs.", "t1", "k8s_logs", {"selector": "app=api", "since": "10m"}),
        _tool_result("t1", logs),
        _tool_use("Fetching metrics.", "t2", "k8s_metrics", {"namespace": "prod"}),
        _tool_result("t2", metrics),
        {"role": "user", "content": "What's the incident root cause and what should I do right now to mitigate?"},
    ]


# ── Scenario 3: API integration — large JSON responses (SmartCrusher target) ──
def _api_integration() -> list[dict]:
    api_resp = json.dumps({
        "data": [{"id": f"usr_{i:05d}", "email": f"user{i}@example.com", "name": f"User {i}",
                  "created_at": "2026-01-01T00:00:00Z", "plan": "pro" if i % 3 else "free",
                  "usage": {"requests": i * 100, "tokens": i * 50000}, "active": i % 7 != 0}
                 for i in range(150)],
        "pagination": {"page": 1, "per_page": 150, "total": 4821},
    }, indent=2)
    return [
        {"role": "user", "content": "Call the users API and tell me how many pro-plan users are inactive."},
        _tool_use("Calling the API.", "t1", "http_get", {"url": "/v1/users?per_page=150"}),
        _tool_result("t1", api_resp),
        {"role": "user", "content": "How many inactive pro users are there?"},
    ]


# ── Scenario 4: short Q&A — no tool output (should NOT compress; passthrough) ──
def _short_qa() -> list[dict]:
    return [
        {"role": "user", "content": "What's the difference between a process and a thread? Keep it short."},
    ]


SCENARIOS: list[dict] = [
    # min_savings floors are set ~halfway below observed Rust-core numbers so the
    # gate catches real regressions without flapping on minor compressor changes.
    {"name": "debugging_session", "description": "grep + read file + run tests (mixed code/log/json tool output)",
     "expect_compressible": True, "min_savings": 0.02, "messages": _debugging_session()},
    {"name": "sre_incident", "description": "production incident — large repetitive logs + metrics JSON",
     "expect_compressible": True, "min_savings": 0.80, "messages": _sre_incident()},
    {"name": "api_integration", "description": "large JSON API response in a tool_result",
     "expect_compressible": True, "min_savings": 0.50, "messages": _api_integration()},
    {"name": "short_qa", "description": "one short user question, no tool output",
     "expect_compressible": False, "min_savings": 0.0, "messages": _short_qa()},
]
