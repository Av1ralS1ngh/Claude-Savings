from __future__ import annotations

"""Client-side license/secret hygiene (decompute_ai.config). The gateway is the authority on
entitlements, so these files are credential caches — the bar is owner-only, atomic, never
half-written, malformed input refused. No anti-tamper (that's the server's job)."""

import stat

import pytest

from decompute_ai import config


def _point_to(tmp, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp)
    monkeypatch.setattr(config, "LICENSE_FILE", tmp / "license.key")
    monkeypatch.setattr(config, "KEY_FILE", tmp / "claude.key")
    monkeypatch.setattr(config, "DEVICE_ID_FILE", tmp / "device.id")
    monkeypatch.setattr(config, "CONFIG_TOML", tmp / "config.toml")


def test_license_key_saved_owner_only_in_locked_dir(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    p = config.save_license_key("DCMP-ABCDEF123456")
    assert p.read_text().strip() == "DCMP-ABCDEF123456"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600          # owner-only file
    assert stat.S_IMODE(config.CONFIG_DIR.stat().st_mode) == 0o700  # owner-only dir
    assert not list(config.CONFIG_DIR.glob("*.tmp"))        # no leftover temp file
    assert config.load_license_key() == "DCMP-ABCDEF123456"


def test_claude_key_saved_owner_only(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    p = config.save_claude_key("sk-ant-abc123")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text().strip() == "sk-ant-abc123"


def test_device_id_saved_owner_only(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    did = config.load_device_id()
    assert did.startswith("dev-")
    assert stat.S_IMODE(config.DEVICE_ID_FILE.stat().st_mode) == 0o600
    # stable across calls
    assert config.load_device_id() == did


@pytest.mark.parametrize("bad", ["", "   ", "has space", "ctrl\x01char", "x" * 257])
def test_malformed_secret_refused_and_nothing_written(tmp_path, monkeypatch, bad):
    _point_to(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        config.save_license_key(bad)
    with pytest.raises(ValueError):
        config.save_claude_key(bad)
    assert not (tmp_path / "license.key").exists()
    assert not (tmp_path / "claude.key").exists()


def test_overwrite_is_atomic_and_clean(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch)
    config.save_license_key("DCMP-one")
    config.save_license_key("DCMP-two")
    assert (tmp_path / "license.key").read_text().strip() == "DCMP-two"
    assert not list(tmp_path.glob("*.tmp"))  # temp swapped in, never left behind
