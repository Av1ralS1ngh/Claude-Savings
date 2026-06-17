from __future__ import annotations

"""Per-harness client tagging: detect_client() buckets the User-Agent, the bucket
(never the raw UA) is persisted, exposed by usage_to_dict, and aggregated as by_client
in usage_summary. Storage normalizes any non-bucket value to NULL (defense-in-depth)."""

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
from decompute_gateway.main import detect_client
from decompute_gateway.models import CLIENT_BUCKETS
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
        raw_input_tokens=1000, yscompress_input_tokens=400, yellowstone_final_input_tokens=400,
        output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        prompt_cache_mode="off", plan=None, request_id="req_x", upstream_request_id=None,
        status_code=200, start_time=time.perf_counter(),
    )
    kw.update(over)
    M._record_usage(**kw)
    return store.recent_usage(project_id="proj_default", limit=1)[0]


# ── detect_client ────────────────────────────────────────────────────────────
def _ua(ua):
    return detect_client(types.SimpleNamespace(headers={"user-agent": ua}))


@pytest.mark.parametrize("ua,expected", [
    ("claude-code/1.0.88", "claude_code"),
    ("Claude Code 1.2", "claude_code"),
    ("claude_code/1.0", "claude_code"),     # underscore variant
    ("claudecode", "claude_code"),          # no-separator variant
    ("OpenClaw/0.5 (acpx)", "openclaw"),
    ("clawdbot", "openclaw"),
    ("anthropic-sdk-python/0.39", "sdk"),
    ("httpx/0.27", "sdk"),
    ("decompute-cli/0.1", "sdk"),
    ("curl/8.4", "other"),
    ("", None),
])
def test_detect_client_buckets(ua, expected):
    assert _ua(ua) == expected


def test_detect_client_only_returns_buckets_or_none():
    for ua in ["claude-code", "openclaw", "httpx", "random-thing", "", "Mozilla/5.0"]:
        v = _ua(ua)
        assert v is None or v in CLIENT_BUCKETS


# ── persistence + exposure ───────────────────────────────────────────────────
def test_client_is_stored_and_exposed_in_usage_dict(store):
    row = _record(store, client="openclaw")
    assert row["client"] == "openclaw"          # usage_to_dict now exposes it


def test_storage_normalizes_non_bucket_to_null(store):
    # A future direct caller passing a raw UA must NOT be persisted as-is.
    row = _record(store, client="Mozilla/5.0 (X11; Linux) raw-user-agent")
    assert row["client"] is None                # normalized away at the storage boundary
    row2 = _record(store, client="claude_code")
    assert row2["client"] == "claude_code"      # a real bucket survives


# ── by_client aggregate ──────────────────────────────────────────────────────
def test_by_client_aggregate_in_usage_summary(store):
    _record(store, client="openclaw", raw_input_tokens=1000, yellowstone_final_input_tokens=300)
    _record(store, client="openclaw", raw_input_tokens=1000, yellowstone_final_input_tokens=300)
    _record(store, client="claude_code", raw_input_tokens=1000, yellowstone_final_input_tokens=900)
    _record(store, client=None, raw_input_tokens=1000, yellowstone_final_input_tokens=500)

    summary = store.usage_summary(project_id="proj_default")
    assert "by_client" in summary
    by = {r["client"]: r for r in summary["by_client"]}

    assert by["openclaw"]["requests"] == 2
    assert by["claude_code"]["requests"] == 1
    assert by["unknown"]["requests"] == 1        # NULL client surfaces as 'unknown'

    # Each row carries token totals + a derived savings percent.
    oc = by["openclaw"]
    assert oc["tokens_saved"] == 1400            # 2 * (1000 - 300)
    assert oc["savings_percent"] == pytest.approx(70.0, abs=0.5)
    assert "estimated_savings_usd" in oc
