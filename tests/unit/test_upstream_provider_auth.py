from __future__ import annotations

from pathlib import Path

from starlette.requests import Request

from decompute_gateway.anthropic_provider import upstream_headers
from decompute_gateway.config import Settings, _default_data_dir
from decompute_gateway.security import upstream_anthropic_auth, upstream_anthropic_key


def _request(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": raw_headers})


def test_client_api_key_forwards_as_x_api_key(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None)
    auth = upstream_anthropic_auth(_request({"x-api-key": "sk-ant-client"}), settings)

    assert auth is not None
    assert auth.kind == "api_key"
    headers = upstream_headers(settings, auth, {})
    assert headers["x-api-key"] == "sk-ant-client"
    assert "authorization" not in headers
    assert upstream_anthropic_key(_request({"x-api-key": "sk-ant-client"}), settings) == "sk-ant-client"


def test_client_bearer_forwards_as_authorization_in_local_mode(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None, environment="local", allow_client_bearer_provider_auth_override=True)
    auth = upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), settings)

    assert auth is not None
    assert auth.kind == "bearer"
    headers = upstream_headers(settings, auth, {})
    assert headers["authorization"] == "Bearer oauth-token"
    assert "x-api-key" not in headers
    assert upstream_anthropic_key(_request({"authorization": "Bearer oauth-token"}), settings) is None


def test_client_bearer_is_blocked_when_disabled(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        anthropic_api_key=None,
        environment="production",
        allow_client_bearer_provider_auth_override=False,
    )

    assert upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), settings) is None


def test_client_bearer_defaults_to_local_only(tmp_path: Path):
    local = Settings(data_dir=tmp_path / "local", anthropic_api_key=None, environment="local")
    prod = Settings(data_dir=tmp_path / "prod", anthropic_api_key=None, environment="production")

    assert upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), local) is not None
    assert upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), prod) is None


def test_static_api_key_wins_over_client_bearer(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key="sk-ant-static", allow_client_bearer_provider_auth_override=True)
    auth = upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), settings)

    assert auth is not None
    assert auth.kind == "api_key"
    assert auth.value == "sk-ant-static"


def test_saved_byok_key_wins_over_client_bearer(tmp_path: Path):
    (tmp_path / "claude.key").write_text("sk-ant-byok\n", encoding="utf-8")
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None, allow_client_bearer_provider_auth_override=True)
    auth = upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), settings)

    assert auth is not None
    assert auth.kind == "api_key"
    assert auth.value == "sk-ant-byok"


def test_dc_platform_bearer_is_not_forwarded_as_provider_auth(tmp_path: Path):
    """A Decompute platform key (dc_*) presented as a bearer is NOT a Claude provider credential — it
    must never be forwarded upstream (Rule 8). Guards the `not bearer.startswith("dc_")` boundary in
    both local (bearer allowed) and prod (bearer blocked) modes."""
    local = Settings(data_dir=tmp_path / "l", anthropic_api_key=None, environment="local", allow_client_bearer_provider_auth_override=True)
    prod = Settings(data_dir=tmp_path / "p", anthropic_api_key=None, environment="production")
    for settings in (local, prod):
        assert upstream_anthropic_auth(_request({"authorization": "Bearer dc_dev_local"}), settings) is None
        assert upstream_anthropic_auth(_request({"authorization": "Bearer dc_prod_abc123"}), settings) is None


def test_subscription_mode_ignores_saved_byok_key(tmp_path: Path):
    (tmp_path / "claude.key").write_text("sk-ant-byok\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text('[claude]\nmode = "claude_subscription"\n', encoding="utf-8")
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None, allow_client_bearer_provider_auth_override=True)
    auth = upstream_anthropic_auth(_request({"authorization": "Bearer oauth-token"}), settings)

    assert auth is not None
    assert auth.kind == "bearer"
    assert auth.value == "oauth-token"


def test_data_dir_falls_back_to_config_dir(tmp_path: Path, monkeypatch):
    """The gateway's data_dir must honor DECOMPUTE_CONFIG_DIR (the CLI's dir) so it reads the SAME
    config.toml the CLI wrote — otherwise subscription mode is silently missed. DATA_DIR/HOME win."""
    monkeypatch.delenv("DECOMPUTE_DATA_DIR", raising=False)
    monkeypatch.delenv("DECOMPUTE_HOME", raising=False)
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(tmp_path / "cli-cfg"))
    assert _default_data_dir() == tmp_path / "cli-cfg"
    # DATA_DIR still takes precedence when both are set.
    monkeypatch.setenv("DECOMPUTE_DATA_DIR", str(tmp_path / "install"))
    assert _default_data_dir() == tmp_path / "install"


# ── OpenClaw / Anthropic-protocol clients: dc_ platform key via x-api-key ──────────
# OpenClaw (api: "anthropic-messages") can only send its credential on x-api-key. A dc_-prefixed value
# there is a Decompute platform key, not a Claude key — accept it for platform auth, never forward it up.

from decompute_gateway.security import platform_api_key_from_request


def test_platform_auth_accepts_dc_key_via_x_api_key():
    raw = platform_api_key_from_request(_request({"x-api-key": "dc_proj_abc123"}))
    assert raw == "dc_proj_abc123"


def test_platform_auth_ignores_real_anthropic_key_via_x_api_key():
    # A real Claude key (sk-ant-…) on x-api-key must NOT be treated as a platform key (it's BYOK).
    raw = platform_api_key_from_request(_request({"x-api-key": "sk-ant-real-byok-key"}))
    assert raw is None


def test_dc_key_via_x_api_key_is_never_forwarded_upstream(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None)
    auth = upstream_anthropic_auth(_request({"x-api-key": "dc_proj_abc123"}), settings)
    assert auth is None  # a platform key must never go to Anthropic


def test_real_anthropic_key_via_x_api_key_still_forwards_upstream(tmp_path: Path):
    # No regression: a genuine sk-ant key on x-api-key is still the BYOK upstream credential.
    settings = Settings(data_dir=tmp_path, anthropic_api_key=None)
    auth = upstream_anthropic_auth(_request({"x-api-key": "sk-ant-byok"}), settings)
    assert auth is not None and auth.kind == "api_key" and auth.value == "sk-ant-byok"
