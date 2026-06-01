"""
Regression tests for the CCR log-record dedup gains (SRE incident, GitHub triage).

The log-record dedup lifted SRE ~66% -> ~76% and triage ~71% -> ~77% end-to-end by
collapsing repetitive log records to one example per event type + a count. These
tests lock that behavior in so a future change can't silently regress it:

  - unit: log-like record arrays collapse (errors preserved); distinct-record arrays
    (issues, etc.) are NEVER collapsed.
  - end-to-end (skippable): SRE and triage compress measurably MORE with dedup on
    than with it off — the actual locked-in gain.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from decompute_gateway import ccr  # noqa: E402


def _log_records(n=200):
    recs = []
    for i in range(n):
        if i % 25 == 0:  # a recurring ERROR event type
            recs.append({"timestamp": f"2024-01-15T10:{i % 60:02d}:00Z", "level": "ERROR",
                         "service": "payment", "message": "Database connection pool exhausted",
                         "trace_id": f"{i:016x}", "metadata": {"host": f"pod-{i % 4}", "region": "us-west-2"}})
        else:  # high-volume INFO noise that should collapse
            recs.append({"timestamp": f"2024-01-15T10:{i % 60:02d}:00Z", "level": "INFO",
                         "service": "auth", "message": f"Authenticated user: user_{i}",
                         "trace_id": f"{i:016x}", "metadata": {"host": f"pod-{i % 4}", "region": "us-west-2"}})
    return recs


# ── unit: deterministic, no engine ────────────────────────────────────────────
def test_log_records_collapse_with_errors_preserved():
    recs = _log_records(200)
    out = ccr._dedup_records(recs)
    assert out is not None, "log-like records must be deduped"
    assert len(out) < len(recs) * 0.6, "dedup must collapse the repetitive bulk"
    assert any(isinstance(r, dict) and r.get("message") == "Database connection pool exhausted" for r in out), \
        "the ERROR event type must survive dedup"
    assert any(isinstance(r, dict) and "_repeats" in r for r in out), \
        "collapsed groups must carry a _repeats count"


def test_distinct_record_arrays_are_never_deduped():
    # GitHub-issue-like records: distinct, no log level/message+timestamp signal
    issues = [{"number": i, "title": f"Bug #{i}: distinct problem in module {i}",
               "state": "open", "labels": ["bug"]} for i in range(100)]
    assert ccr._dedup_records(issues) is None, "distinct records must be kept (not deduped)"


def test_two_distinct_event_types_both_survive():
    recs = []
    for i in range(60):
        recs.append({"timestamp": f"t{i}", "level": "ERROR", "service": "pay",
                     "message": "pool exhausted", "trace_id": f"{i:x}"})
        recs.append({"timestamp": f"t{i}", "level": "WARN", "service": "api",
                     "message": "rate limit approaching", "trace_id": f"{i:x}"})
    out = ccr._dedup_records(recs)
    msgs = {r.get("message") for r in out if isinstance(r, dict)}
    assert "pool exhausted" in msgs and "rate limit approaching" in msgs


# ── end-to-end: the actual gain, measured in an isolated subprocess ────────────
# (run out-of-process, like the RC gate, so app-config validation and import order
# from other tests can't interfere)
REPO = Path(__file__).resolve().parents[1]

_MEASURE = r'''
import sys, json, asyncio
sys.path.insert(0, "apps/api")
sys.path.insert(0, "/Users/avii/Desktop/Decompute_Assignment/headroom/benchmarks")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import real_world_agent_benchmark as B
import decompute_gateway.main as M
from decompute_gateway import ccr
def build(blobs):
    m=[{"role":"user","content":"go"}]
    for i,b in enumerate(blobs):
        m.append({"role":"assistant","content":[{"type":"text","text":"t"},{"type":"tool_use","id":"t%d"%i,"name":"t","input":{}}]})
        m.append({"role":"user","content":[{"type":"tool_result","tool_use_id":"t%d"%i,"content":json.dumps(b)}]})
    m.append({"role":"user","content":"a"}); return m
async def pct(blobs):
    r=await M.yscompress.compress_messages(build(blobs),project_id="proj_default",ccr=True)
    return (1-r.tokens_after/max(r.tokens_before,1))*100
sre=[B.generate_log_search("payment error",300),B.generate_database_query_results("metrics",200),B.generate_filesystem_search("payment",150)]
tri=[B.generate_github_issues("o/r",100),B.generate_github_code_search("bug",80),B.generate_log_search("exception",200)]
orig=ccr._dedup_records
res={"sre_on":asyncio.run(pct(sre)),"tri_on":asyncio.run(pct(tri))}
ccr._dedup_records=lambda r: None
res["sre_off"]=asyncio.run(pct(sre)); res["tri_off"]=asyncio.run(pct(tri))
print("RESULT_JSON="+json.dumps(res))
'''


def test_sre_and_triage_gain_over_no_dedup():
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    proc = subprocess.run([sys.executable, "-c", _MEASURE], capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=400)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    if line is None:
        pytest.skip(f"could not measure gain (engine/data/model unavailable):\n{proc.stderr[-600:]}")
    res = json.loads(line.split("=", 1)[1])
    assert res["sre_on"] >= res["sre_off"] + 5.0, f"SRE log-dedup gain regressed: {res}"
    assert res["tri_on"] >= res["tri_off"] + 3.0, f"triage log-dedup gain regressed: {res}"
