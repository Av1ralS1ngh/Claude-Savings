from __future__ import annotations

import importlib
from pathlib import Path


def test_gateway_entrypoint_env_dirs_prefer_install_home(monkeypatch, tmp_path: Path):
    entrypoint = importlib.import_module("decompute_gateway.__main__")
    home = tmp_path / "home"
    install_home = tmp_path / "custom-home"
    config_home = tmp_path / "config-home"

    monkeypatch.setattr(entrypoint.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("DECOMPUTE_HOME", str(install_home))
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(config_home))

    assert entrypoint._env_dirs() == [install_home, config_home, home / ".decompute"]


def test_gateway_entrypoint_env_dirs_deduplicate(monkeypatch, tmp_path: Path):
    entrypoint = importlib.import_module("decompute_gateway.__main__")
    home = tmp_path / "home"
    install_home = tmp_path / "same"

    monkeypatch.setattr(entrypoint.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("DECOMPUTE_HOME", str(install_home))
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(install_home))

    assert entrypoint._env_dirs() == [install_home, home / ".decompute"]
