"""BYOK CLI + config tests — the `decompute init/claude/uninstall` rewrite.

Covers the security-relevant invariants of the bring-your-own-key model:
  - non-secret settings live in config.toml; the Claude key lives in claude.key (chmod 600)
  - the key is NEVER printed by config-show and NEVER written to a project file by `vscode`
  - the SDK forwards the key as x-api-key only when set (else local_noauth, no auth header)
  - uninstall keeps config/data/key by default; only --purge removes them
  - the old dc_-key surface (login / keys / api-key-create) is gone

These don't need a running gateway; the live launch sequence is covered by the smoke test.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Reload config + cli with ~/.decompute redirected into a tmp dir, key/base env cleared."""
    cfgdir = tmp_path / ".decompute"
    claude_dir = tmp_path / ".claude"
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(cfgdir))
    monkeypatch.setenv("DECOMPUTE_CLAUDE_CONFIG_DIR", str(claude_dir))
    for k in ("DECOMPUTE_CLAUDE_KEY", "ANTHROPIC_API_KEY", "DECOMPUTE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    config = importlib.reload(importlib.import_module("decompute_ai.config"))
    importlib.reload(importlib.import_module("decompute_ai.client"))
    cli = importlib.reload(importlib.import_module("decompute_ai.cli"))
    return config, cli, cfgdir


# ── config.toml + claude.key separation ────────────────────────────────────────

def test_config_toml_roundtrip_preserves_defaults(env):
    config, _, cfgdir = env
    config.save_config({"gateway": {"base_url": "http://127.0.0.1:9000"}, "ccr": {"enabled": False}})
    cfg = config.load_config()
    assert config.base_url() == "http://127.0.0.1:9000"
    assert cfg["ccr"]["enabled"] is False            # override applied
    assert cfg["ccr"]["retention_days"] == 7         # default preserved by deep-merge
    assert cfg["privacy"]["pii_mode"] == "tokenize"  # untouched section keeps defaults
    assert "[gateway]" in (cfgdir / "config.toml").read_text()


def test_claude_key_is_secret_and_chmod_600(env):
    config, _, cfgdir = env
    config.save_claude_key("sk-ant-secret")
    keyfile = cfgdir / "claude.key"
    assert config.load_claude_key() == "sk-ant-secret"
    assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    # The secret must never land in the non-secret config.toml.
    assert "sk-ant-secret" not in (cfgdir / "config.toml").read_text() if (cfgdir / "config.toml").exists() else True
    config.clear_claude_key()
    assert config.load_claude_key() is None


def test_legacy_json_is_migrated(env):
    config, _, cfgdir = env
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "config.json").write_text(json.dumps({"base_url": "http://old:8080", "last_session_id": "cc-old"}))
    cfg = config.load_config()
    assert config.base_url() == "http://old:8080"
    assert cfg["claude"]["last_session_id"] == "cc-old"


def test_env_overrides_keyfile(env, monkeypatch):
    config, _, _ = env
    config.save_claude_key("sk-ant-from-file")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert config.load_claude_key() == "sk-ant-from-env"  # env wins


# ── BYOK SDK headers ────────────────────────────────────────────────────────────

def test_client_sends_x_api_key_only_when_set(env):
    config, _, _ = env
    from decompute_ai.client import Decompute
    assert "x-api-key" not in Decompute().headers()       # no key → local_noauth, no auth header
    config.save_claude_key("sk-ant-byok")
    h = Decompute().headers()
    assert h["x-api-key"] == "sk-ant-byok"
    assert "x-decompute-api-key" not in h                 # the old dc_ header is gone


# ── init wizard (non-interactive) ───────────────────────────────────────────────

def test_init_yes_writes_config_and_key(env):
    config, cli, cfgdir = env
    res = runner.invoke(cli.app, ["init", "--yes", "--claude-key", "sk-ant-demo", "--base-url", "http://127.0.0.1:8080"])
    assert res.exit_code == 0, res.stdout
    assert (cfgdir / "config.toml").exists()
    assert config.load_claude_key() == "sk-ant-demo"
    assert config.load_config()["claude"]["mode"] == "anthropic_api"


def test_init_subscription_mode_writes_no_key_and_warns_about_old_key(env, monkeypatch):
    config, cli, cfgdir = env
    config.save_claude_key("sk-ant-existing")
    monkeypatch.setattr(cli, "_gateway_running", lambda *_a, **_k: True)

    res = runner.invoke(cli.app, ["init", "--yes", "--auth-mode", "claude_subscription", "--base-url", "http://127.0.0.1:8080"])

    assert res.exit_code == 0, res.stdout
    assert config.load_config()["claude"]["mode"] == "claude_subscription"
    assert config.load_claude_key() == "sk-ant-existing"
    assert "ignores it and keeps it for later" in res.stdout
    assert "sk-ant-existing" not in (cfgdir / "config.toml").read_text()


def test_telemetry_toggle_on_off_status(env):
    """`decompute telemetry on|off|status` flips the [telemetry].enabled flag and reports it; status
    makes the privacy contract visible (counts only, never prompts)."""
    config, cli, _ = env
    # default status: on, and it must say it never sends content
    res = runner.invoke(cli.app, ["telemetry"])
    assert res.exit_code == 0, res.stdout
    assert "on" in res.stdout.lower()
    assert "never sent" in res.stdout.lower() and "prompts" in res.stdout.lower()
    # off persists to config.toml and is reflected
    res = runner.invoke(cli.app, ["telemetry", "off"])
    assert res.exit_code == 0
    assert config.load_config()["telemetry"]["enabled"] is False
    assert "off" in runner.invoke(cli.app, ["telemetry", "status"]).stdout.lower()
    # back on
    runner.invoke(cli.app, ["telemetry", "on"])
    assert config.load_config()["telemetry"]["enabled"] is True


def test_init_rerun_preserves_existing_byok_mode_interactively(env, monkeypatch):
    """Re-running `decompute init` interactively (no --auth-mode) must NOT flip an existing BYOK user
    to subscription. Pressing Enter keeps their configured mode (regression: default used to be [1])."""
    config, cli, _ = env
    config.save_claude_key("sk-ant-existing")
    config.save_config({"claude": {"mode": "anthropic_api"}})
    monkeypatch.setattr(cli, "_gateway_running", lambda *_a, **_k: True)
    # The user already has a key on disk; pass --claude-key so the API-key branch doesn't re-prompt.
    # Empty stdin then accepts each remaining default — crucially the auth-mode prompt defaults to [2]
    # (the existing mode), NOT [1], so the mode is preserved rather than flipped to subscription.
    res = runner.invoke(
        cli.app, ["init", "--base-url", "http://127.0.0.1:8080", "--claude-key", "sk-ant-existing"], input="\n\n\n\n\n"
    )
    assert res.exit_code == 0, res.stdout
    assert "Current setup: anthropic_api" in res.stdout            # the existing mode was offered as default
    assert config.load_config()["claude"]["mode"] == "anthropic_api"  # mode preserved, not flipped


def test_init_subscription_rejects_non_loopback_gateway(env):
    """Existing Claude plan auth is local-only; init must refuse a remote gateway in subscription
    mode (mirrors `configure claude-code`)."""
    _, cli, _ = env
    res = runner.invoke(cli.app, [
        "init", "--yes", "--auth-mode", "claude_subscription", "--base-url", "https://gateway.decompute.run",
    ])
    assert res.exit_code == 2
    assert "local-only" in res.stdout


def test_subscription_mode_auth_headers_ignore_saved_api_key(env):
    config, cli, _ = env
    config.save_claude_key("sk-ant-existing")
    config.save_config({"claude": {"mode": "claude_subscription"}})

    assert "x-api-key" not in cli._auth_headers()


def test_config_show_never_prints_the_key(env):
    config, cli, _ = env
    config.save_claude_key("sk-ant-TOPSECRET")
    res = runner.invoke(cli.app, ["config-show"])
    assert res.exit_code == 0
    assert "sk-ant-TOPSECRET" not in res.stdout
    assert '"claude_key_set": true' in res.stdout


# ── vscode never writes the key into a project file ─────────────────────────────

def test_vscode_omits_key_from_settings(env, tmp_path):
    config, cli, _ = env
    config.save_claude_key("sk-ant-leakcheck")
    config.save_config({"gateway": {"base_url": "http://127.0.0.1:8080"}})
    proj = tmp_path / "proj"
    proj.mkdir()
    res = runner.invoke(cli.app, ["vscode", str(proj)])
    assert res.exit_code == 0, res.stdout
    settings = (proj / ".vscode" / "settings.json").read_text()
    assert "sk-ant-leakcheck" not in settings           # the key must NOT be in a committable file
    assert "ANTHROPIC_API_KEY" not in settings
    assert "x-decompute-session-id" in settings          # routing IS written


def test_configure_claude_code_defaults_to_hosted_without_provider_key(env):
    config, cli, _ = env
    res = runner.invoke(cli.app, ["configure", "claude-code", "--yes"])
    assert res.exit_code == 0, res.stdout
    settings_path = Path(os.environ["DECOMPUTE_CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings = json.loads(settings_path.read_text())
    env_block = settings["env"]
    assert env_block["ANTHROPIC_BASE_URL"] == "https://gateway.decompute.run"
    assert "x-decompute-device:" in env_block["ANTHROPIC_CUSTOM_HEADERS"]
    assert "x-decompute-session-id:" in env_block["ANTHROPIC_CUSTOM_HEADERS"]
    assert "ANTHROPIC_API_KEY" not in settings_path.read_text()
    assert "sk-ant" not in settings_path.read_text()
    assert config.base_url() == "https://gateway.decompute.run"


def test_configure_claude_code_subscription_mode_is_local_and_no_key(env):
    config, cli, _ = env
    config.save_claude_key("sk-ant-existing")
    res = runner.invoke(cli.app, [
        "configure", "claude-code",
        "--yes",
        "--auth-mode", "claude_subscription",
        "--gateway-url", "http://127.0.0.1:8080",
    ])
    assert res.exit_code == 0, res.stdout
    settings_path = Path(os.environ["DECOMPUTE_CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings_text = settings_path.read_text()
    settings = json.loads(settings_text)
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"
    assert "ANTHROPIC_API_KEY" not in settings_text
    assert "sk-ant-existing" not in settings_text
    assert config.load_config()["claude"]["mode"] == "claude_subscription"
    assert config.load_claude_key() == "sk-ant-existing"
    assert "Existing API key credentials will be ignored in subscription mode" in res.stdout


def test_configure_claude_code_subscription_rejects_hosted_gateway(env):
    _, cli, _ = env
    res = runner.invoke(cli.app, [
        "configure", "claude-code",
        "--yes",
        "--auth-mode", "claude_subscription",
        "--gateway-url", "https://gateway.decompute.run",
    ])
    assert res.exit_code == 2
    assert "local-only" in res.stdout


# ── uninstall: safe by default, destructive only on --purge ─────────────────────

def test_uninstall_yes_keeps_config_and_key(env, monkeypatch):
    config, cli, _ = env
    config.save_claude_key("sk-ant-keepme")
    config.save_config({"gateway": {"base_url": "http://127.0.0.1:8080"}})
    monkeypatch.setattr(cli, "_stop_gateway", lambda *_a, **_k: False)  # don't touch a real gateway
    res = runner.invoke(cli.app, ["uninstall", "--yes"])
    assert res.exit_code == 0, res.stdout
    assert config.load_claude_key() == "sk-ant-keepme"   # key kept
    assert config.CONFIG_TOML.exists()                   # config kept


def test_uninstall_purge_removes_config_and_key(env, monkeypatch):
    config, cli, _ = env
    config.save_claude_key("sk-ant-purge")
    config.save_config({"gateway": {"base_url": "http://127.0.0.1:8080"}})
    monkeypatch.setattr(cli, "_stop_gateway", lambda *_a, **_k: False)
    res = runner.invoke(cli.app, ["uninstall", "--purge"])
    assert res.exit_code == 0, res.stdout
    assert config.load_claude_key() is None
    assert not config.CONFIG_TOML.exists()


# ── the dc_-key surface is gone ─────────────────────────────────────────────────

def test_no_dc_key_commands(env):
    _, cli, _ = env
    names = {c.name for c in cli.app.registered_commands}
    assert "login" not in names
    assert "api-key-create" not in names
    assert "keys" not in {g.name for g in cli.app.registered_groups}
