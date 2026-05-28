from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the Decompute Claude Optimization Gateway."""

    app_name: str = "Decompute Gateway"
    environment: str = os.getenv("DECOMPUTE_ENV", "local")
    host: str = os.getenv("DECOMPUTE_HOST", "127.0.0.1")
    port: int = int(os.getenv("DECOMPUTE_PORT", "8080"))

    # Storage
    data_dir: Path = Path(os.getenv("DECOMPUTE_DATA_DIR", ".decompute"))
    sqlite_path: Path = Path(os.getenv("DECOMPUTE_SQLITE_PATH", ".decompute/decompute.db"))

    # Platform auth. Keep disabled for Claude Code local proxy flows by default.
    require_auth: bool = os.getenv("DECOMPUTE_REQUIRE_AUTH", "false").lower() == "true"
    dev_api_key: str = os.getenv("DECOMPUTE_API_KEY", "dc_dev_local")
    allow_client_provider_key: bool = os.getenv("DECOMPUTE_ALLOW_CLIENT_PROVIDER_KEY", "true").lower() == "true"

    # Anthropic upstream. If ANTHROPIC_API_KEY is unset, the proxy tries incoming x-api-key/authorization.
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    anthropic_base_url: str = os.getenv("ANTHROPIC_BASE_URL_UPSTREAM", "https://api.anthropic.com")
    anthropic_version: str = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    mock_anthropic: bool = os.getenv("DECOMPUTE_MOCK_ANTHROPIC", "true").lower() == "true"

    # Headroom integration. If absent or unavailable, fallback compression is used.
    yscompress_base_url: str | None = os.getenv("YSCOMPRESS_BASE_URL")
    yscompress_timeout_s: float = float(os.getenv("YSCOMPRESS_TIMEOUT_S", "8"))
    yscompress_default_mode: str = os.getenv("YSCOMPRESS_MODE", "safe")
    yscompress_ccr_enabled: bool = os.getenv("YSCOMPRESS_CCR", "true").lower() == "true"

    # Prompt caching
    prompt_cache_mode: str = os.getenv("DECOMPUTE_PROMPT_CACHE", "auto")  # off|auto|passthrough

    # Response metadata behavior
    return_proxy_metadata: bool = os.getenv("DECOMPUTE_RETURN_PROXY_METADATA", "false").lower() == "true"

    # Token/cost estimation. These are placeholders and should be made configurable per plan.
    default_input_cost_per_mtok: float = float(os.getenv("DECOMPUTE_INPUT_COST_PER_MTOK", "3.0"))
    default_output_cost_per_mtok: float = float(os.getenv("DECOMPUTE_OUTPUT_COST_PER_MTOK", "15.0"))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
