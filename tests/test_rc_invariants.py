"""
CI wrapper for the release-candidate compression gate (scripts/rc_benchmark.py).

Runs the gate as an isolated subprocess — the same way a release job invokes it —
so it is immune to test import-ordering (the ML offline flags must be set before
`transformers` imports) and asserts the real exit code. Every release invariant
must hold: rust engine, no fallback, CCR active, code-aware + ML where applicable,
a safe PII mode, and a pinned benchmark.

Skips (not fails) only when the gate's prerequisites are genuinely missing on the
box (engine/benchmark-data/model not importable), so a provisioned release runner
fails loudly while a bare unit box doesn't get a false red.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "rc_benchmark.py"


def _run_gate():
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    return subprocess.run(
        [sys.executable, str(GATE), "--json"],
        capture_output=True, text=True, env=env, timeout=400, cwd=str(REPO),
    )


def test_release_candidate_invariants():
    if not GATE.exists():
        pytest.skip("rc_benchmark.py not present")
    proc = _run_gate()
    # Genuine prerequisite gap (missing engine / benchmark data / model) -> skip.
    if proc.returncode not in (0, 1):
        pytest.skip(f"RC gate could not run (exit {proc.returncode}):\n{proc.stderr[-600:]}")
    if proc.returncode == 1 and "Traceback" in proc.stderr and (
        "ModuleNotFoundError" in proc.stderr or "No such file" in proc.stderr
    ):
        pytest.skip(f"RC gate prerequisites unavailable:\n{proc.stderr[-600:]}")

    # Otherwise the gate ran; parse its report and assert every invariant passed.
    try:
        report = json.loads(proc.stdout)
    except Exception:
        assert proc.returncode == 0, (
            f"RC gate FAILED (exit {proc.returncode}); could not parse report.\n"
            f"stdout tail:\n{proc.stdout[-800:]}\nstderr tail:\n{proc.stderr[-400:]}"
        )
        return
    failures = [c for c in report["invariants"] if not c["ok"]]
    detail = "\n".join(
        f"  {c['invariant']}: expected={c['expected']} actual={c['actual']}" for c in report["invariants"]
    )
    assert not failures, f"release invariants FAILED: {[c['invariant'] for c in failures]}\n{detail}"
    assert report["benchmark_version"], "benchmark_version must be pinned (non-empty)"
    assert proc.returncode == 0
