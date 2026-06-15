"""Anonymous savings telemetry — the PRIVACY-CRITICAL invariants.

The product's whole pitch is local-first/BYOK privacy, so these tests treat "we never send prompts or
content" as a hard, code-enforced guarantee — not a promise. They also cover the opt-outs (off /
DO_NOT_TRACK) and the weekly cadence so telemetry can never become chatty or block the gateway.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from decompute_gateway import telemetry as t


# ── the payload is COUNTS ONLY ──────────────────────────────────────────────────

def test_build_payload_is_counts_only():
    p = t.build_payload(
        {"requests": 100, "tokens_saved": 50000, "raw_input_tokens": 70000, "estimated_savings_usd": 1.2345},
        device="dev-abc", version="0.2.0", now=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )
    assert set(p.keys()) == {"device", "week", "requests", "tokens_saved", "tokens_before", "est_savings_usd", "os", "version"}
    assert p["requests"] == 100 and p["tokens_saved"] == 50000 and p["tokens_before"] == 70000
    assert p["week"] == "2026-W25"
    # every value is a scalar count/string — no nested structures that could hide content
    assert all(isinstance(v, (str, int, float)) for v in p.values())


def test_assert_rejects_any_content_field():
    # A future bug that tried to attach a prompt/message/file/content key must raise, not send.
    for bad in ("prompt", "prompts", "message", "messages", "content", "text", "file", "files", "input", "system", "body", "model"):
        with pytest.raises(ValueError):
            t._assert_counts_only({"device": "x", "requests": 1, bad: "secret user data"})


def test_assert_rejects_non_scalar_values():
    with pytest.raises(ValueError):
        t._assert_counts_only({"device": "x", "requests": [1, 2, 3]})
    with pytest.raises(ValueError):
        t._assert_counts_only({"device": "x", "requests": {"nested": "obj"}})


def test_iso_week_format():
    assert t._iso_week(datetime(2026, 1, 1, tzinfo=timezone.utc)).startswith("20")
    assert t._iso_week(datetime(2026, 6, 16, tzinfo=timezone.utc)) == "2026-W25"


# ── opt-outs + cadence (maybe_send_weekly never sends when it shouldn't) ─────────

class _FakeSettings:
    def __init__(self, tmp, *, enabled=True, interval=7, url="http://example.invalid"):
        self.data_dir = tmp
        self.telemetry_enabled = enabled
        self.telemetry_interval_days = interval
        self.telemetry_url = url


class _FakeStore:
    def __init__(self, requests=10):
        self.calls = 0
        self._requests = requests

    def usage_summary(self, *, project_id, since=None):
        self.calls += 1
        return {"requests": self._requests, "tokens_saved": 123, "raw_input_tokens": 456, "estimated_savings_usd": 0.5}


def _no_network(monkeypatch):
    """Replace the actual POST so a test never hits the network; record whether it was called."""
    sent = {}
    def fake_post(url, payload, *, timeout=5.0):
        sent["url"] = url
        sent["payload"] = payload
        return True
    monkeypatch.setattr(t, "_post", fake_post)
    return sent


def _run_inline(monkeypatch):
    """Run the daemon-thread body synchronously so the test is deterministic."""
    started = []
    class _Inline:
        def __init__(self, target, **k):
            self._t = target
        def start(self):
            started.append(True)
            self._t()
    monkeypatch.setattr(t.threading, "Thread", _Inline)
    return started


def test_disabled_sends_nothing(monkeypatch, tmp_path):
    sent = _no_network(monkeypatch)
    _run_inline(monkeypatch)
    t.maybe_send_weekly(_FakeSettings(tmp_path, enabled=False), _FakeStore(), version="0.2.0")
    assert "payload" not in sent  # telemetry_enabled=False -> nothing sent


def test_config_opt_out_blocks_send(monkeypatch, tmp_path):
    # `decompute telemetry off` writes [telemetry].enabled=false to config.toml.
    (tmp_path / "config.toml").write_text('[telemetry]\nenabled = false\n', encoding="utf-8")
    sent = _no_network(monkeypatch)
    _run_inline(monkeypatch)
    t.maybe_send_weekly(_FakeSettings(tmp_path, enabled=True), _FakeStore(), version="0.2.0")
    assert "payload" not in sent


def test_weekly_cadence_gates_repeat_sends(monkeypatch, tmp_path):
    sent = _no_network(monkeypatch)
    _run_inline(monkeypatch)
    s = _FakeSettings(tmp_path, enabled=True, interval=7)
    store = _FakeStore()
    # first call sends + records last_sent
    t.maybe_send_weekly(s, store, version="0.2.0")
    assert sent.get("payload") and sent["payload"]["requests"] == 10
    first_payload = sent["payload"]
    sent.clear()
    # immediate second call is within the interval -> no send
    t.maybe_send_weekly(s, store, version="0.2.0")
    assert "payload" not in sent
    # force=True overrides the cadence
    t.maybe_send_weekly(s, store, version="0.2.0", force=True)
    assert sent.get("payload")
    # the payload never carried content
    t._assert_counts_only(first_payload)


def test_no_requests_yet_records_but_does_not_send(monkeypatch, tmp_path):
    sent = _no_network(monkeypatch)
    _run_inline(monkeypatch)
    t.maybe_send_weekly(_FakeSettings(tmp_path), _FakeStore(requests=0), version="0.2.0", force=True)
    assert "payload" not in sent                       # nothing to report
    assert (tmp_path / "telemetry.json").exists()      # but last_sent recorded so we don't recompute hourly


def test_send_records_last_sent_state(monkeypatch, tmp_path):
    _no_network(monkeypatch)
    _run_inline(monkeypatch)
    t.maybe_send_weekly(_FakeSettings(tmp_path), _FakeStore(), version="0.2.0", force=True)
    state = json.loads((tmp_path / "telemetry.json").read_text())
    assert state["last_sent_epoch"] > 0
