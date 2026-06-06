from __future__ import annotations

"""P0 #2: the recorded savings metric must be honest — measured on Anthropic's real tokenizer
(count_tokens raw baseline vs billed compressed input), not the len/4 estimator that undercounts
~5x. These pin the reconciliation at the single chokepoint (_record_usage): billed + real_raw
override the estimate; cache is grossed correctly; and we fall back to the estimate (never zero)
when reality is unavailable (mock/error/cancelled-stream/CCR).
"""

import os
import tempfile
import time
import types

os.environ.setdefault("DECOMPUTE_DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mktemp(suffix='.db')}")
os.environ.setdefault("DECOMPUTE_ENV", "local")
os.environ.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "true")
os.environ.setdefault("DECOMPUTE_REQUIRE_AUTH", "false")

import pytest

import decompute_gateway.main as M
from decompute_gateway.pricing import cost_usd, savings_usd
from test_billing import _settings, _store  # bare import (pytest prepend importmode)

MODEL = "claude-haiku-4-5"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    st = _store(s)
    monkeypatch.setattr(M, "store", st)
    monkeypatch.setattr(M, "settings", s)
    return st


def _record(store, **over):
    principal = types.SimpleNamespace(project_id="proj_default", key_prefix="dc_test")
    kw = dict(
        principal=principal, endpoint="/v1/messages", model=MODEL,
        raw_input_tokens=151, yscompress_input_tokens=151, yellowstone_final_input_tokens=151,
        output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        prompt_cache_mode="off", plan=None, request_id="req_x", upstream_request_id=None,
        status_code=200, start_time=time.perf_counter(),
    )
    kw.update(over)
    M._record_usage(**kw)
    return store.recent_usage(project_id="proj_default", limit=1)[0]


def test_billed_and_real_raw_override_the_estimate(store):
    # The estimator claimed raw=151/final=151 (-> ~0 real savings). Reality: count_tokens says the
    # raw prompt is 1000 tokens and Anthropic BILLED 800 for the compressed one.
    row = _record(store, raw_input_tokens=151, yellowstone_final_input_tokens=151,
                  billed_input_tokens=800, real_raw_input_tokens=1000)
    assert row["raw_input_tokens"] == 1000               # real baseline, not the 151 estimate
    assert row["yellowstone_final_input_tokens"] == 800  # billed, not the 151 estimate
    assert row["tokens_saved"] == 200                    # 1000 - 800, honest
    assert row["estimated_savings_usd"] == savings_usd(
        model=MODEL, raw_input_tokens=1000, final_input_tokens=800, output_tokens=20)
    assert row["estimated_savings_usd"] > 0
    assert row["compression_ratio"] == 800 / 1000


def test_fallback_to_estimate_when_no_real_data(store):
    # mock / error / cancelled-stream / CCR: no billed and no real_raw -> keep the estimate as-is,
    # never zero the row.
    row = _record(store, raw_input_tokens=900, yellowstone_final_input_tokens=151,
                  billed_input_tokens=None, real_raw_input_tokens=None)
    assert row["raw_input_tokens"] == 900
    assert row["yellowstone_final_input_tokens"] == 151
    assert row["tokens_saved"] == 749


def test_billed_is_grossed_with_cache_for_cost(store):
    # Anthropic's input_tokens EXCLUDES cache. eff_final = billed + cache (gross), so cost_usd's
    # internal `uncached = input - cache` nets back to the billed 800 — no cache double-subtract.
    row = _record(store, billed_input_tokens=800, real_raw_input_tokens=1500,
                  cache_creation_input_tokens=0, cache_read_input_tokens=200)
    assert row["yellowstone_final_input_tokens"] == 1000   # 800 billed + 200 cache_read
    assert row["tokens_saved"] == 500                       # 1500 - 1000
    assert row["estimated_cost_usd"] == cost_usd(
        model=MODEL, input_tokens=1000, output_tokens=20, cache_read_input_tokens=200)
    # That cost reflects exactly 800 uncached + 200 cache-read tokens (not 1000 uncached).
    assert row["estimated_cost_usd"] == cost_usd(
        model=MODEL, input_tokens=800, output_tokens=20) + cost_usd(
        model=MODEL, input_tokens=200, output_tokens=0, cache_read_input_tokens=200)


def test_real_raw_only_still_uses_estimate_final(store):
    # Defensive: real_raw present but billed missing -> raw real, final stays estimate (same as
    # the CCR/partial path); never crashes, never negative.
    row = _record(store, raw_input_tokens=151, yellowstone_final_input_tokens=120,
                  real_raw_input_tokens=1000, billed_input_tokens=None)
    assert row["raw_input_tokens"] == 1000
    assert row["yellowstone_final_input_tokens"] == 120
    assert row["tokens_saved"] == 880
