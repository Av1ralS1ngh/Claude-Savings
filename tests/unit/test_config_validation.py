import importlib

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


# ── Teacher-student production safety (Phase 1) ────────────────────────────────
# These exercise validate_for_startup directly by constructing a production Settings with the unsafe
# field set, which avoids the importlib.reload env dance above. Each asserts the specific guard fires.


def _prod_settings(**overrides) -> Settings:
    """A minimally-valid production Settings (auth/db/secrets satisfied) plus the given overrides, so
    a single unsafe teacher-student field is the only thing that can fail validation."""
    base = dict(
        environment="production",
        require_auth_env=True,
        dashboard_auth_required_env=True,
        database_url="postgresql+psycopg://u:p@db/x",
        mock_anthropic=False,
        api_key_hash_salt="prod-salt-1",
        device_fingerprint_salt="prod-salt-2",
        license_key_hash_salt="prod-salt-3",
    )
    base.update(overrides)
    return Settings(**base)


def test_prod_baseline_is_valid():
    # The baseline (no teacher-student overrides) must pass, so the failures below are attributable.
    _prod_settings().validate_for_startup()


def test_prod_rejects_local_student_mock():
    with pytest.raises(RuntimeError, match="DECOMPUTE_LOCAL_STUDENT_MOCK"):
        _prod_settings(local_student_mock=True).validate_for_startup()


def test_prod_rejects_remote_plain_http_student():
    with pytest.raises(RuntimeError, match="loopback"):
        _prod_settings(
            local_student_enabled=True,
            local_student_base_url="http://10.0.0.9:18080/v1",
        ).validate_for_startup()


def test_prod_allows_loopback_http_student():
    # A loopback http:// student is fine; only a REMOTE plain-http endpoint leaks cleartext.
    _prod_settings(
        local_student_enabled=True,
        local_student_base_url="http://127.0.0.1:18080/v1",
    ).validate_for_startup()


def test_prod_rejects_test_execution_without_sandbox():
    with pytest.raises(RuntimeError, match="DECOMPUTE_TS_SANDBOX_BACKEND"):
        _prod_settings(
            teacher_student_test_execution_enabled=True,
            teacher_student_sandbox_backend="disabled",
        ).validate_for_startup()


def test_prod_rejects_unknown_sandbox_backend():
    with pytest.raises(RuntimeError, match="not a .*production-safe sandbox"):
        _prod_settings(
            teacher_student_test_execution_enabled=True,
            teacher_student_sandbox_backend="bare_subprocess",
        ).validate_for_startup()


def test_prod_rejects_unsafe_container_runtime():
    with pytest.raises(RuntimeError, match="DECOMPUTE_TS_SANDBOX_RUNTIME"):
        _prod_settings(
            teacher_student_test_execution_enabled=True,
            teacher_student_sandbox_backend="container",
            teacher_student_sandbox_runtime="sh",
        ).validate_for_startup()


def test_prod_allows_container_with_docker():
    _prod_settings(
        teacher_student_test_execution_enabled=True,
        teacher_student_sandbox_backend="container",
        teacher_student_sandbox_runtime="docker",
    ).validate_for_startup()


def test_prod_rejects_claude_training_without_signoff():
    with pytest.raises(RuntimeError, match="SIGNOFF"):
        _prod_settings(
            teacher_student_allow_claude_training_targets=True,
            teacher_student_claude_training_signoff=False,
        ).validate_for_startup()


def test_prod_allows_claude_training_with_signoff():
    _prod_settings(
        teacher_student_allow_claude_training_targets=True,
        teacher_student_claude_training_signoff=True,
    ).validate_for_startup()


def test_prod_rejects_redacted_capture_without_encryption_key():
    with pytest.raises(RuntimeError, match="DECOMPUTE_CCR_ENCRYPTION_KEY"):
        _prod_settings(
            teacher_student_capture_enabled=True,
            teacher_student_governance_mode="redacted",
            ccr_encryption_key=None,
        ).validate_for_startup()
