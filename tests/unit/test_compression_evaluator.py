"""
§17 Compression-safety evaluator.

Runs the full golden-fixture corpus (tests/fixtures/compression/) through the real
gateway compressor and asserts the safe-mode launch gate: for every fixture we know

  - raw tokens, compressed tokens, compression ratio   (accounting)
  - compression NEVER inflates                          (tokens_after <= tokens_before)
  - the latest user instruction survives verbatim       (never compress away the ask)
  - critical facts survive the safe-mode summarizer      (no fixture loses a key error)
  - secrets are never persisted to CCR                   (scanner redacts / withholds)

Launch gate (checklist §17): no fixture loses a critical fact in safe mode.

    pytest -q  tests/unit/test_compression_evaluator.py        # gate
    pytest -s  tests/unit/test_compression_evaluator.py        # + savings report
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from decompute_gateway.config import Settings
from decompute_gateway.crypto import CCRCrypto
from decompute_gateway.db import Store
from decompute_gateway.secret_scanner import SecretScanner
from decompute_gateway.yscompress import YSCompressClient, estimate_tokens, summarize_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compression"

# A trailing instruction appended after each blob fixture. It is the LATEST user
# message, so it must come back byte-for-byte after compression.
ASK = "Using only the context above, tell me the single root cause in one sentence."


# filename, role to wrap the blob as, critical markers that must survive safe mode,
# whether the fixture is itself a full conversation, whether it carries secrets.
CORPUS = [
    {"file": "stack_trace_python.txt", "role": "tool", "markers": ["PermissionError", "Traceback"]},
    {"file": "npm_install_error.txt", "role": "tool", "markers": ["ERESOLVE"]},
    {"file": "pytest_failure.txt", "role": "tool", "markers": ["AssertionError", "FAILED"]},
    {"file": "giant_json_api_response.json", "role": "tool", "markers": ["rate_limit_exceeded"]},
    {"file": "kubernetes_logs.txt", "role": "tool", "markers": ["OOMKilled", "CrashLoopBackOff"]},
    {"file": "code_diff_large.patch", "role": "tool", "markers": ["anthropic_provider.py"]},
    {"file": "rag_chunks_mixed.md", "role": "tool", "markers": ["Postgres 15"]},
    {"file": "long_conversation.json", "role": "user", "markers": [], "conversation": True},
    {"file": "secrets_should_redact.env", "role": "tool", "markers": [], "secrets": True},
]
IDS = [c["file"] for c in CORPUS]


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> YSCompressClient:
    db = tmp_path_factory.mktemp("eval") / "eval.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{db}")
    store = Store(settings)
    store.create_schema_for_local_dev()
    return YSCompressClient(settings, store, CCRCrypto(None, "local-v1"), SecretScanner())


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _messages_for(case: dict) -> tuple[list[dict], str]:
    """Returns (messages, latest_user_text) for a fixture case."""
    if case.get("conversation"):
        messages = json.loads(_read(case["file"]))
        latest = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return messages, latest
    blob = _read(case["file"])
    return [{"role": case["role"], "content": blob}, {"role": "user", "content": ASK}], ASK


def _compress(client: YSCompressClient, messages: list[dict]):
    return asyncio.run(client.compress_messages(messages, project_id="proj_default", mode="safe"))


def _latest_user_text(messages: list[dict]) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return None


@pytest.mark.parametrize("case", CORPUS, ids=IDS)
def test_fixture_is_present_and_nonempty(case: dict):
    path = FIXTURES / case["file"]
    assert path.exists(), f"missing compression fixture: {case['file']}"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("case", CORPUS, ids=IDS)
def test_compression_accounting_and_never_inflates(client: YSCompressClient, case: dict):
    messages, _ = _messages_for(case)
    raw_tokens = estimate_tokens(messages)
    assert raw_tokens > 0

    result = _compress(client, messages)

    # Accounting is well-defined and consistent.
    assert result.tokens_before == raw_tokens
    assert result.tokens_after >= 1
    assert 0.0 < result.compression_ratio <= 1.0
    # Never inflate — compression is a win or a no-op, never a penalty.
    assert result.tokens_after <= result.tokens_before, (
        f"{case['file']}: inflated {result.tokens_before} -> {result.tokens_after}"
    )


@pytest.mark.parametrize("case", CORPUS, ids=IDS)
def test_latest_user_instruction_preserved_verbatim(client: YSCompressClient, case: dict):
    messages, expected = _messages_for(case)
    result = _compress(client, messages)
    if expected:
        assert _latest_user_text(result.messages) == expected, (
            f"{case['file']}: latest user instruction was altered by compression"
        )


@pytest.mark.parametrize("case", [c for c in CORPUS if c["markers"]], ids=[c["file"] for c in CORPUS if c["markers"]])
def test_critical_facts_survive_safe_mode_summary(case: dict):
    """The launch gate: the deterministic safe-mode summarizer must keep every
    critical fact (error lines land in <important_lines>, head, or tail)."""
    summary = summarize_text(_read(case["file"]), max_chars=1000)
    for marker in case["markers"]:
        assert marker in summary, f"{case['file']}: safe-mode summary dropped critical fact {marker!r}"


def test_secrets_are_never_persisted_to_ccr():
    """A fixture carrying credentials must be detected and redacted; no raw secret
    material may appear in a CCR-storable representation."""
    scanner = SecretScanner()
    text = _read("secrets_should_redact.env")
    findings = scanner.scan(text)
    assert findings, "secret scanner failed to detect credentials in fixture"

    raw_secrets = ["sk-ant-abcdefghijklmnopqrstuvwxyz123456", "user:password@example.com"]
    redacted = scanner.redact(text, findings)
    withheld = scanner.summarize(text, findings)
    for secret in raw_secrets:
        assert secret not in redacted, f"redaction leaked secret: {secret!r}"
        assert secret not in withheld, f"CCR summary leaked secret: {secret!r}"


def test_evaluator_report(client: YSCompressClient, capsys):
    """Not a gate — prints the per-fixture evaluation table for optimization runs."""
    rows = []
    for case in CORPUS:
        messages, expected = _messages_for(case)
        result = _compress(client, messages)
        pct = (1 - result.tokens_after / max(result.tokens_before, 1)) * 100
        summary = summarize_text(_read(case["file"]), max_chars=1000)
        facts_ok = all(m in summary for m in case["markers"]) if case["markers"] else True
        ask_ok = (not expected) or (_latest_user_text(result.messages) == expected)
        rows.append((case["file"], result.tokens_before, result.tokens_after, pct, facts_ok, ask_ok, result.engine))

    with capsys.disabled():
        print("\n" + "─" * 96)
        print(f"  {'fixture':<30}{'raw':>7}{'sent':>7}{'saved':>8}  facts  ask   engine")
        print("─" * 96)
        for name, before, after, pct, facts_ok, ask_ok, engine in rows:
            print(f"  {name:<30}{before:>7,}{after:>7,}{pct:>7.1f}%   {'OK ' if facts_ok else 'LOSS':>4}  {'OK ' if ask_ok else 'BAD':>3}   {engine}")
        print("─" * 96)
    assert rows
