import importlib
import os

import pytest


def test_production_refuses_sqlite_and_mock(monkeypatch):
    monkeypatch.setenv("DECOMPUTE_ENV", "production")
    monkeypatch.setenv("DECOMPUTE_DATABASE_URL", "sqlite+pysqlite:///.decompute/test.db")
    monkeypatch.setenv("DECOMPUTE_MOCK_ANTHROPIC", "true")
    monkeypatch.setenv("DECOMPUTE_REQUIRE_AUTH", "true")
    monkeypatch.setenv("DECOMPUTE_DASHBOARD_AUTH_REQUIRED", "true")
    monkeypatch.setenv("DECOMPUTE_CCR_ENABLED", "false")
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    with pytest.raises(RuntimeError):
        cfg.get_settings()
