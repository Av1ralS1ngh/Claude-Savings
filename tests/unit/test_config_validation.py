import importlib
import os

import pytest

from decompute_gateway.config import Settings


def test_local_gateway_defaults_to_real_upstream_when_key_is_present():
    assert Settings().mock_anthropic is False


def test_production_refuses_sqlite_and_mock(monkeypatch):
    monkeypatch.setenv("DECOMPUTE_ENV", "production")
    monkeypatch.setenv("DECOMPUTE_DATABASE_URL", "sqlite+pysqlite:///.decompute/test.db")
    monkeypatch.setenv("DECOMPUTE_MOCK_ANTHROPIC", "true")
    monkeypatch.setenv("DECOMPUTE_REQUIRE_AUTH", "true")
    monkeypatch.setenv("DECOMPUTE_DASHBOARD_AUTH_REQUIRED", "true")
    monkeypatch.setenv("DECOMPUTE_CCR_ENABLED", "false")
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    try:
        with pytest.raises(RuntimeError):
            cfg.get_settings()
    finally:
        # Settings reads env into dataclass field defaults at class-definition time,
        # so reloading under the production env above poisons the module for every
        # later test that builds Settings (e.g. importing decompute_gateway.main).
        # Revert the env and reload once more to restore clean local defaults.
        monkeypatch.undo()
        importlib.reload(cfg)
