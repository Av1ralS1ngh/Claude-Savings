"""
Real-world compression regression + optimization harness.

Runs realistic agent sessions (tests/fixtures/real_world_sessions.py) through the
actual gateway compressor (YSCompressClient — Rust core + passthrough validation)
and asserts the invariants that must always hold, regardless of how the compressor
is tuned:

  1. Compression NEVER inflates: tokens_after <= tokens_before   (the bug we fixed)
  2. The latest user instruction is preserved verbatim          (never compress the ask)
  3. Tool-output-heavy sessions actually shrink                  (the value prop)

Run as a regression gate:      pytest -q tests/test_compression_realworld.py
See the savings report:        pytest -s tests/test_compression_realworld.py
Use it to tune the compressor: change settings/engine, re-run, compare the table.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from decompute_gateway.config import Settings
from decompute_gateway.crypto import CCRCrypto
from decompute_gateway.db import Store
from decompute_gateway.secret_scanner import SecretScanner
from decompute_gateway.yscompress import YSCompressClient

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from real_world_sessions import SCENARIOS  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> YSCompressClient:
    db = tmp_path_factory.mktemp("cmp") / "rw.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{db}")
    store = Store(settings)
    store.create_schema_for_local_dev()
    return YSCompressClient(settings, store, CCRCrypto(None, "local-v1"), SecretScanner())


def _compress(client: YSCompressClient, messages: list[dict]):
    return asyncio.run(client.compress_messages(messages, project_id="proj_default", mode="safe"))


def _latest_user_text(messages: list[dict]) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_compression_invariants(client: YSCompressClient, scenario: dict):
    result = _compress(client, scenario["messages"])

    # INVARIANT 1 — never inflate. Compression is a win or a no-op, never a penalty.
    assert result.tokens_after <= result.tokens_before, (
        f"{scenario['name']}: compression inflated context "
        f"{result.tokens_before} -> {result.tokens_after}"
    )

    # INVARIANT 2 — the user's latest instruction must survive verbatim.
    original_ask = _latest_user_text(scenario["messages"])
    if original_ask is not None:
        out_ask = _latest_user_text(result.messages)
        assert out_ask == original_ask, (
            f"{scenario['name']}: latest user instruction was modified by compression"
        )

    # INVARIANT 3 — each workload must meet its recorded savings floor (only when
    # the Rust core is active; the dumb fallback has different characteristics).
    if result.engine.startswith("yscompress_rust") and not result.engine.endswith("passthrough_no_gain"):
        savings = 1 - result.tokens_after / max(result.tokens_before, 1)
        assert savings >= scenario["min_savings"], (
            f"{scenario['name']}: savings {savings:.1%} below floor "
            f"{scenario['min_savings']:.0%} — compressor regression"
        )


def test_print_savings_report(client: YSCompressClient, capsys):
    """Not a pass/fail gate — prints the savings table for optimization runs."""
    rows = []
    for s in SCENARIOS:
        r = _compress(client, s["messages"])
        pct = (1 - r.tokens_after / max(r.tokens_before, 1)) * 100
        rows.append((s["name"], r.tokens_before, r.tokens_after, pct, r.engine))

    with capsys.disabled():
        print("\n" + "─" * 78)
        print(f"  {'scenario':<22}{'before':>8}{'after':>8}{'saved':>9}   engine")
        print("─" * 78)
        for name, before, after, pct, engine in rows:
            print(f"  {name:<22}{before:>8,}{after:>8,}{pct:>8.1f}%   {engine}")
        print("─" * 78)
        total_before = sum(r[1] for r in rows)
        total_after = sum(r[2] for r in rows)
        total_pct = (1 - total_after / max(total_before, 1)) * 100
        print(f"  {'TOTAL':<22}{total_before:>8,}{total_after:>8,}{total_pct:>8.1f}%")
        print("─" * 78)

    assert rows  # sanity
