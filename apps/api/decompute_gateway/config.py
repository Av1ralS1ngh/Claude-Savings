from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv
load_dotenv(override=False)  # load .env file if present; shell env vars take precedence


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = "Decompute Gateway"
    environment: str = os.getenv("DECOMPUTE_ENV", "local").lower()
    host: str = os.getenv("DECOMPUTE_HOST", "127.0.0.1")
    port: int = _int("DECOMPUTE_PORT", 8080)
    data_dir: Path = Path(os.getenv("DECOMPUTE_DATA_DIR", ".decompute"))

    database_url: str = os.getenv(
        "DECOMPUTE_DATABASE_URL",
        os.getenv("DATABASE_URL", "sqlite+pysqlite:///.decompute/decompute.db"),
    )
    db_pool_size: int = _int("DECOMPUTE_DB_POOL_SIZE", 5)
    db_max_overflow: int = _int("DECOMPUTE_DB_MAX_OVERFLOW", 10)

    require_auth_env: bool = _bool("DECOMPUTE_REQUIRE_AUTH", False)
    dev_api_key: str = os.getenv("DECOMPUTE_API_KEY", "dc_dev_local")
    dashboard_auth_required_env: bool = _bool("DECOMPUTE_DASHBOARD_AUTH_REQUIRED", False)
    allow_client_provider_key: bool = _bool("DECOMPUTE_ALLOW_CLIENT_PROVIDER_KEY", True)
    api_key_hash_salt: str = os.getenv("DECOMPUTE_API_KEY_HASH_SALT", "local-dev-salt-change-me")

    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    anthropic_base_url: str = os.getenv("ANTHROPIC_BASE_URL_UPSTREAM", "https://api.anthropic.com")
    anthropic_version: str = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    allowed_anthropic_beta_headers: list[str] = field(default_factory=lambda: _csv("DECOMPUTE_ALLOWED_ANTHROPIC_BETAS", ""))
    mock_anthropic: bool = _bool("DECOMPUTE_MOCK_ANTHROPIC", True)

    yscompress_base_url: str | None = os.getenv("YSCOMPRESS_BASE_URL") or os.getenv("HEADROOM_BASE_URL")
    yscompress_timeout_s: float = _float("YSCOMPRESS_TIMEOUT_S", _float("HEADROOM_TIMEOUT_S", 8.0))
    yscompress_default_mode: str = os.getenv("YSCOMPRESS_MODE", os.getenv("HEADROOM_MODE", "safe"))
    yscompress_ccr_enabled: bool = _bool("YSCOMPRESS_CCR", _bool("HEADROOM_CCR", False))
    yscompress_bypass_header: str = "x-yscompress-bypass"

    ccr_enabled: bool = _bool("DECOMPUTE_CCR_ENABLED", yscompress_ccr_enabled)
    ccr_retention_days: int = _int("DECOMPUTE_CCR_RETENTION_DAYS", 7)
    ccr_redaction_mode: str = os.getenv("DECOMPUTE_CCR_REDACTION_MODE", "metadata_only_on_secret")
    # How detected PII is rewritten before it can reach Claude or the store. Secrets
    # (credentials/keys) are ALWAYS hard-redacted regardless of this setting.
    #   "tokenize" -> <email_1> stable pseudonym (default; keeps referential structure)
    #   "redact"   -> [REDACTED:<kind>]            (stricter; removes PII entirely)
    # Anything else is treated as "redact" (never passthrough). Release gate requires
    # one of these two safe modes.
    pii_mode: str = os.getenv("DECOMPUTE_PII_MODE", "tokenize")
    ccr_encryption_key: str | None = os.getenv("DECOMPUTE_CCR_ENCRYPTION_KEY")
    ccr_key_id: str = os.getenv("DECOMPUTE_CCR_KEY_ID", "local-v1")

    prompt_cache_mode: str = os.getenv("DECOMPUTE_PROMPT_CACHE", "auto")

    # Privacy-safe support bundle (decompute support bundle / POST /v1/support/bundle).
    # Defaults the bundle's PII mode to the gateway-wide pii_mode so they stay aligned.
    support_bundle_enabled: bool = _bool("DECOMPUTE_SUPPORT_BUNDLE_ENABLED", True)
    support_bundle_output_dir: Path = Path(os.getenv("DECOMPUTE_SUPPORT_BUNDLE_DIR", "")) if os.getenv("DECOMPUTE_SUPPORT_BUNDLE_DIR") else Path(os.getenv("DECOMPUTE_DATA_DIR", ".decompute")) / "support"
    support_bundle_pii_mode: str = os.getenv("DECOMPUTE_SUPPORT_BUNDLE_PII_MODE", os.getenv("DECOMPUTE_PII_MODE", "tokenize"))
    support_bundle_max_log_bytes: int = int(os.getenv("DECOMPUTE_SUPPORT_BUNDLE_MAX_LOG_BYTES", "250000"))
    support_bundle_include_env: bool = _bool("DECOMPUTE_SUPPORT_BUNDLE_INCLUDE_ENV", True)
    support_bundle_include_logs: bool = _bool("DECOMPUTE_SUPPORT_BUNDLE_INCLUDE_LOGS", True)
    support_bundle_include_reports: bool = _bool("DECOMPUTE_SUPPORT_BUNDLE_INCLUDE_REPORTS", True)

    cors_origins: list[str] = field(default_factory=lambda: _csv("DECOMPUTE_CORS_ORIGINS", "http://127.0.0.1:8080,http://localhost:8080"))
    cors_allow_credentials: bool = _bool("DECOMPUTE_CORS_ALLOW_CREDENTIALS", True)
    security_headers: bool = _bool("DECOMPUTE_SECURITY_HEADERS", True)

    max_request_bytes: int = _int("DECOMPUTE_MAX_REQUEST_BYTES", 10 * 1024 * 1024)
    max_context_tokens: int = _int("DECOMPUTE_MAX_CONTEXT_TOKENS", 200_000)
    per_key_rpm: int = _int("DECOMPUTE_PER_KEY_RPM", 60)
    project_tokens_per_min: int = _int("DECOMPUTE_PROJECT_TOKENS_PER_MIN", 200_000)
    max_concurrent_streams_per_project: int = _int("DECOMPUTE_MAX_CONCURRENT_STREAMS", 10)
    monthly_spend_usd: float = _float("DECOMPUTE_MONTHLY_SPEND_USD", 250.0)

    # ── Billing / licensing (Stripe) ──────────────────────────────────────────
    # Billing is OFF unless a Stripe secret key is set, so local/dev never touches Stripe.
    stripe_secret_key: str | None = os.getenv("DECOMPUTE_STRIPE_SECRET_KEY")
    stripe_webhook_secret: str | None = os.getenv("DECOMPUTE_STRIPE_WEBHOOK_SECRET")
    stripe_price_id: str | None = os.getenv("DECOMPUTE_STRIPE_PRICE_ID", "price_1Te0pKFbGwqVxzy7wyp1yQ1d")
    stripe_product_id: str | None = os.getenv("DECOMPUTE_STRIPE_PRODUCT_ID", "prod_UdHOGNcyVN2V4e")
    # The tier granted by a successful subscription to stripe_price_id.
    stripe_price_tier: str = os.getenv("DECOMPUTE_STRIPE_PRICE_TIER", "pro")
    # Pin the Stripe API version for deterministic payloads (optional; None = account default).
    stripe_api_version: str | None = os.getenv("DECOMPUTE_STRIPE_API_VERSION")
    # Grace after a subscription period lapses before a missed-deletion webhook self-heals to free.
    billing_grace_days: int = _int("DECOMPUTE_BILLING_GRACE_DAYS", 3)
    billing_success_url: str = os.getenv("DECOMPUTE_BILLING_SUCCESS_URL", "https://decompute.ai/billing/success")
    billing_cancel_url: str = os.getenv("DECOMPUTE_BILLING_CANCEL_URL", "https://decompute.ai/billing/cancel")
    billing_portal_return_url: str = os.getenv("DECOMPUTE_BILLING_PORTAL_RETURN_URL", "https://decompute.ai/account")

    # ── Device licensing (BYOK/self-hosted axis) ──────────────────────────────
    # Monetizes the compression itself for self-hosted BYOK users (who are otherwise
    # unmetered). A device presents an HMAC'd fingerprint; an active Pro license keeps
    # compression on, otherwise a per-device daily free quota applies and degrades to
    # mode='off' (still forwards to Claude) once spent. Orthogonal to the project tiers.
    device_licensing_enabled: bool = _bool("DECOMPUTE_DEVICE_LICENSING_ENABLED", True)
    # Dedicated HMAC pepper for device fingerprints — NEVER reuse api_key_hash_salt, and
    # never the dev default in production (see validate_for_startup).
    device_fingerprint_salt: str = os.getenv("DECOMPUTE_DEVICE_FINGERPRINT_SALT", "local-dev-device-salt-change-me")
    # Pepper for at-rest hashing of issued license keys (only the hash is stored).
    license_key_hash_salt: str = os.getenv("DECOMPUTE_LICENSE_KEY_HASH_SALT", "local-dev-license-salt-change-me")
    # Free tier: N compressed requests per device per window before degrading to no-compression.
    free_compression_quota: int = _int("DECOMPUTE_FREE_COMPRESSION_QUOTA", 100)
    free_quota_window: str = os.getenv("DECOMPUTE_FREE_QUOTA_WINDOW", "daily")  # daily | lifetime
    device_license_tier: str = os.getenv("DECOMPUTE_DEVICE_LICENSE_TIER", "pro")
    # Purchase site the upgrade modal/CLI sends the user to, and the app deep-link to return to.
    pro_upgrade_url: str = os.getenv("DECOMPUTE_PRO_UPGRADE_URL", "https://decompute.ai/pro")
    pro_upgrade_return_to: str | None = os.getenv("DECOMPUTE_PRO_UPGRADE_RETURN_TO", "decompute://license/activate")
    license_max_devices: int = _int("DECOMPUTE_LICENSE_MAX_DEVICES", 1)

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    # Retry transient upstream failures (500/504/529) with exponential backoff.
    upstream_max_retries: int = _int("DECOMPUTE_UPSTREAM_MAX_RETRIES", 2)
    upstream_retry_backoff_s: float = _float("DECOMPUTE_UPSTREAM_RETRY_BACKOFF_S", 0.25)

    allowed_models: list[str] = field(default_factory=lambda: _csv("DECOMPUTE_ALLOWED_MODELS", "claude-sonnet,claude-opus,claude-haiku"))
    return_proxy_metadata: bool = _bool("DECOMPUTE_RETURN_PROXY_METADATA", False)

    yellowstone_policy_version: str = os.getenv("YELLOWSTONE_POLICY_VERSION", "v0.1")
    yellowstone_planner_version: str = os.getenv("YELLOWSTONE_PLANNER_VERSION", "v0.1")
    local_preprocess_enabled: bool = _bool("YELLOWSTONE_LOCAL_PREPROCESS_ENABLED", False)
    local_student_enabled: bool = _bool("DECOMPUTE_LOCAL_STUDENT_ENABLED", False)
    local_student_training_enabled: bool = _bool("DECOMPUTE_LOCAL_STUDENT_TRAINING_ENABLED", False)

    @property
    def is_production(self) -> bool:
        return self.environment in {"prod", "production", "private_beta", "staging"}

    @property
    def require_auth(self) -> bool:
        return self.is_production or self.require_auth_env

    @property
    def dashboard_auth_required(self) -> bool:
        return self.is_production or self.dashboard_auth_required_env

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            rel = self.database_url.split("///", 1)[-1]
            Path(rel).parent.mkdir(parents=True, exist_ok=True)

    def model_allowed(self, model: str) -> bool:
        if not self.allowed_models:
            return True
        lower = (model or "").lower()
        return any(allowed.lower() in lower for allowed in self.allowed_models)

    def validate_for_startup(self) -> None:
        errors: list[str] = []
        if self.is_production:
            if not self.require_auth:
                errors.append("DECOMPUTE_REQUIRE_AUTH must be true in production")
            if self.mock_anthropic:
                errors.append("DECOMPUTE_MOCK_ANTHROPIC must be false in production")
            if self.uses_sqlite:
                errors.append("DECOMPUTE_DATABASE_URL must be Postgres in production; SQLite is local-only")
            if "*" in self.cors_origins:
                errors.append("DECOMPUTE_CORS_ORIGINS cannot contain * in production")
            if self.cors_allow_credentials and any(o == "*" for o in self.cors_origins):
                errors.append("Wildcard CORS with credentials is forbidden")
            if self.ccr_enabled and not self.ccr_encryption_key:
                errors.append("DECOMPUTE_CCR_ENCRYPTION_KEY is required when CCR is enabled in production")
            if not self.dashboard_auth_required:
                errors.append("DECOMPUTE_DASHBOARD_AUTH_REQUIRED must be true in production")
            if self.local_student_training_enabled:
                errors.append("Local student continuous training is not a V0 production feature")
            # Secret hygiene: hashing/signing peppers must never ship as the dev default in prod,
            # or device-hashes/license-hashes become forgeable and low-entropy fingerprints
            # become brute-forceable from a DB leak.
            for name, value, default in (
                ("DECOMPUTE_API_KEY_HASH_SALT", self.api_key_hash_salt, "local-dev-salt-change-me"),
                ("DECOMPUTE_DEVICE_FINGERPRINT_SALT", self.device_fingerprint_salt, "local-dev-device-salt-change-me"),
                ("DECOMPUTE_LICENSE_KEY_HASH_SALT", self.license_key_hash_salt, "local-dev-license-salt-change-me"),
            ):
                if not value or value == default:
                    errors.append(f"{name} must be set to a non-default secret in production")
        if errors:
            joined = "\n - ".join(errors)
            raise RuntimeError(f"Invalid Decompute production configuration:\n - {joined}")


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    s.validate_for_startup()
    return s
