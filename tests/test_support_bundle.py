from __future__ import annotations

"""Support-bundle tests.

Ported from decompute_support_bundle_pack (the sys.path shim is dropped — this repo's
pyproject sets pythonpath=["apps/api", ...]). Adds gateway-specific coverage: the admin
router rejects non-admin callers, records an audit event, and returns leak-scan metadata.
"""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import fastapi
from fastapi.testclient import TestClient

from decompute_gateway.support_bundle import SecretAndPIIScrubber, build_support_bundle, scan_bundle_for_leaks
from decompute_gateway.support_router import SupportBundleRequest, include_support_bundle_router, is_support_admin
import pytest as _pytest
from pydantic import ValidationError


def read_zip_texts(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            try:
                out[name] = zf.read(name).decode("utf-8")
            except UnicodeDecodeError:
                pass
    return out


# ── scrubber / builder (ported) ──────────────────────────────────────────────

def test_scrubber_redacts_common_secrets() -> None:
    scrubber = SecretAndPIIScrubber()
    raw = """
    ANTHROPIC_API_KEY=sk-ant-fakefakefakefakefakefake
    Authorization: Bearer eyJabcabcabc.eyJdefdefdef.sigsignaturesig
    x-api-key: ghp_abcdefghijklmnopqrstuvwxyz123456
    DATABASE_URL=postgres://alice:supersecret@localhost:5432/app
    -----BEGIN PRIVATE KEY-----
    abcdef
    -----END PRIVATE KEY-----
    """
    scrubbed = scrubber.scrub_text(raw)
    assert "sk-ant-fake" not in scrubbed
    assert "ghp_" not in scrubbed
    assert "supersecret" not in scrubbed
    assert "BEGIN PRIVATE KEY" not in scrubbed
    assert "[REDACTED" in scrubbed


def test_pii_tokenized_stably() -> None:
    scrubber = SecretAndPIIScrubber(pii_mode="tokenize")
    raw = "alice@example.com called from 198.51.100.10, then alice@example.com retried."
    scrubbed = scrubber.scrub_text(raw)
    assert "alice@example.com" not in scrubbed
    assert "198.51.100.10" not in scrubbed
    assert scrubbed.count("<email_1>") == 2
    assert "<ip_1>" in scrubbed


def test_scrub_json_omits_prompt_like_fields() -> None:
    scrubber = SecretAndPIIScrubber()
    payload = {
        "request_id": "req_1",
        "messages": [{"role": "user", "content": "raw user prompt secret"}],
        "original_text": "raw CCR payload",
        "api_key": "sk-ant-fakefakefakefake",
        "content_type": "application/json",
    }
    scrubbed = scrubber.scrub_jsonish(payload)
    assert scrubbed["messages"] == "[OMITTED:raw_prompt_or_payload_field]"
    assert scrubbed["original_text"] == "[OMITTED:raw_prompt_or_payload_field]"
    assert scrubbed["api_key"] == "[REDACTED:secret_field]"
    assert scrubbed["content_type"] == "application/json"


def test_support_bundle_excludes_raw_prompts_and_ccr_payload(tmp_path: Path) -> None:
    data = tmp_path / ".decompute"
    logs = data / "logs"
    ccr = data / "ccr_store"
    reports = tmp_path / "docs" / "releases" / "v0.1.0-beta.1"
    logs.mkdir(parents=True)
    ccr.mkdir(parents=True)
    reports.mkdir(parents=True)

    raw_prompt = "PLEASE_DO_NOT_INCLUDE_THIS_RAW_PROMPT"
    fake_secret = "sk-ant-fakefakefakefakefakefake"
    fake_email = "alice@example.com"

    (logs / "gateway.log").write_text(
        f"request_id=req_123 {fake_email} x-api-key: {fake_secret}\nraw_prompt={raw_prompt}\n",
        encoding="utf-8",
    )
    (ccr / "ccr_payload.json").write_text(
        json.dumps({"original_text": raw_prompt, "api_key": fake_secret}),
        encoding="utf-8",
    )
    (reports / "benchmark.json").write_text(
        json.dumps({"workload": "code_search", "raw_prompt": raw_prompt, "savings": 94}),
        encoding="utf-8",
    )

    result = build_support_bundle(
        output_dir=data / "support",
        data_dir=data,
        project_root=tmp_path,
        extra_canaries=[raw_prompt, fake_secret, fake_email],
    )
    assert result.leak_scan["ok"], result.leak_scan

    texts = read_zip_texts(result.path)
    joined = "\n".join(texts.values())
    assert raw_prompt not in joined
    assert fake_secret not in joined
    assert fake_email not in joined
    assert "[REDACTED" in joined
    assert "<email_1>" in joined
    assert not any("ccr_payload" in name for name in texts)
    assert "manifest.json" in texts
    assert "privacy_report.json" in texts


def test_support_bundle_manifest_and_privacy_report(tmp_path: Path) -> None:
    data = tmp_path / ".decompute"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "gateway.log").write_text("ok request_id=req_1\n", encoding="utf-8")
    result = build_support_bundle(output_dir=data / "support", data_dir=data, project_root=tmp_path)
    texts = read_zip_texts(result.path)
    manifest = json.loads(texts["manifest.json"])
    privacy = json.loads(texts["privacy_report.json"])
    assert manifest["raw_prompts_included"] is False
    assert manifest["ccr_payloads_included"] is False
    assert privacy["database_dump_included"] is False
    assert result.path.exists()


def test_bundle_scan_fails_on_canary(tmp_path: Path) -> None:
    data = tmp_path / ".decompute"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "gateway.log").write_text("canary=VISIBLE_CANARY\n", encoding="utf-8")
    result = build_support_bundle(
        output_dir=data / "support",
        data_dir=data,
        project_root=tmp_path,
        extra_canaries=["VISIBLE_CANARY"],
    )
    assert not result.leak_scan["ok"]
    assert any(item["reason"] == "canary_found" for item in result.leak_scan["suspicious"])


# ── admin router (gateway-specific) ──────────────────────────────────────────

def test_is_support_admin_scope_logic() -> None:
    assert is_support_admin({"auth_mode": "dev"}) is True
    assert is_support_admin({"scopes": ["admin"]}) is True
    assert is_support_admin({"scopes": ["support:write"]}) is True
    assert is_support_admin({"auth_mode": "api_key", "scopes": ["messages:write"]}) is False


class _FakeStore:
    """Minimal store: a metadata-only snapshot + an audit recorder that captures calls."""

    def __init__(self) -> None:
        self.audit_calls: list[dict] = []

    def support_bundle_snapshot(self) -> dict:
        return {"counts": {"projects": 1}, "usage_totals": {"output_tokens": 0}}

    def record_audit(self, **kwargs) -> None:
        self.audit_calls.append(kwargs)


def _mk_client(tmp_path: Path, monkeypatch, ctx: dict) -> tuple[TestClient, _FakeStore]:
    monkeypatch.chdir(tmp_path)  # isolate project_root=cwd() so the bundle doesn't scan the repo
    (tmp_path / ".decompute" / "logs").mkdir(parents=True)
    settings = SimpleNamespace(
        data_dir=tmp_path / ".decompute",
        support_bundle_enabled=True,
        support_bundle_output_dir=tmp_path / ".decompute" / "support",
        app_name="Decompute Gateway",
        environment="local",
        mock_anthropic=True,
        prompt_cache_mode="auto",
    )
    store = _FakeStore()
    app = fastapi.FastAPI()
    include_support_bundle_router(
        app, store=store, settings=settings,
        auth_context_func=lambda request: ctx,
        capabilities_func=lambda: {"yscompress": {"active": True}},
    )
    return TestClient(app), store


def test_router_rejects_non_admin(tmp_path: Path, monkeypatch) -> None:
    client, _ = _mk_client(tmp_path, monkeypatch, {"auth_mode": "api_key", "scopes": ["messages:write"]})
    resp = client.post("/v1/support/bundle", json={"download": False})
    assert resp.status_code == 403


def test_router_admin_records_audit_and_returns_metadata(tmp_path: Path, monkeypatch) -> None:
    client, store = _mk_client(tmp_path, monkeypatch, {"auth_mode": "dev", "scopes": ["admin"], "project_id": "proj_default", "key_prefix": "dc_dev"})
    resp = client.post("/v1/support/bundle", json={"download": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["leak_scan"]["ok"] is True
    assert body["manifest"]["raw_prompts_included"] is False
    # Audit event recorded with the leak-scan result.
    assert len(store.audit_calls) == 1
    call = store.audit_calls[0]
    assert call["action"] == "support_bundle.create"
    assert call["metadata"]["leak_scan_ok"] is True


# ── review-driven leak regressions ───────────────────────────────────────────

def test_encryption_key_and_opaque_secrets_are_redacted() -> None:
    """The CCR master key (base64 Fernet, opaque to value-regexes) must be redacted by
    field/name, in JSON and free text — leaking it == leaking every CCR payload."""
    s = SecretAndPIIScrubber()
    key = "oQWIYLid7ZE1TG6Q44gPeZ_Sr8V5hr9ye4on7m2RYtE="
    assert s.scrub_jsonish({"DECOMPUTE_CCR_ENCRYPTION_KEY": key})["DECOMPUTE_CCR_ENCRYPTION_KEY"].startswith("[REDACTED")
    assert key not in s.scrub_text(f"DECOMPUTE_CCR_ENCRYPTION_KEY={key}")
    assert s.scrub_jsonish({"signing_key": "abcd1234abcd1234"})["signing_key"].startswith("[REDACTED")


def test_dotted_card_and_ssn_are_tokenized() -> None:
    s = SecretAndPIIScrubber()
    assert "4111.1111.1111.1111" not in s.scrub_text("card 4111.1111.1111.1111")
    assert "123.45.6789" not in s.scrub_text("ssn 123.45.6789")


def test_prompt_under_benign_key_and_long_prose_are_omitted() -> None:
    s = SecretAndPIIScrubber()
    assert s.scrub_jsonish({"note": "the user's confidential prompt"})["note"].startswith("[OMITTED")
    assert s.scrub_jsonish({"input": "x " * 200})["input"] == "[OMITTED:long_text_value]"


def test_store_snapshot_metadata_is_not_over_omitted() -> None:
    """Numeric metric fields (output_tokens / raw_input_tokens) must survive — the
    prompt-key denylist must not collide with metadata names."""
    s = SecretAndPIIScrubber()
    snap = {"usage_totals": {"output_tokens": 682, "raw_input_tokens": 110690, "distinct_models": 2}}
    out = s.scrub_jsonish(snap)["usage_totals"]
    assert out == {"output_tokens": 682, "raw_input_tokens": 110690, "distinct_models": 2}


def test_scanner_catches_unredacted_secret_field_independently(tmp_path: Path) -> None:
    """The leak scanner must flag a secret-named field with a long unredacted value
    even when no value-regex matches it (so it does not share the scrubber's blind spot)."""
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("env.json", json.dumps({"DECOMPUTE_CCR_ENCRYPTION_KEY": "oQWIYLid7ZE1TG6Q44gPeZ_Sr8V5hr9ye4on7m2RYtE="}))
    res = scan_bundle_for_leaks(z)
    assert res["ok"] is False
    assert any(x["reason"] == "unredacted_secret_field" for x in res["suspicious"])


def test_router_rejects_passthrough_pii_mode() -> None:
    with _pytest.raises(ValidationError):
        SupportBundleRequest(pii_mode="passthrough")


def test_failed_bundle_is_quarantined_not_in_output_root(tmp_path: Path) -> None:
    data = tmp_path / ".decompute"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "gateway.log").write_text("canary=VISIBLE_CANARY\n", encoding="utf-8")
    out = data / "support"
    result = build_support_bundle(output_dir=out, data_dir=data, project_root=tmp_path, extra_canaries=["VISIBLE_CANARY"])
    assert result.leak_scan["ok"] is False
    # The unsafe zip must NOT sit in the shareable output root.
    assert not list(out.glob("*.zip"))
    assert result.path.parent.name == "quarantine"
    assert result.path.exists()
