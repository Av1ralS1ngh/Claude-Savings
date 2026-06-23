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


def _bool_opt(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
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


def _default_data_dir() -> Path:
    """The gateway's data directory — ALWAYS absolute.

    launchd starts the installed gateway with cwd=/ (read-only). A relative default like
    ".decompute" makes ensure_dirs() do mkdir("/.decompute") → OSError [Errno 30], so the gateway
    crashes before it binds and Claude Code — routed at a port nothing is listening on — appears to
    "think" forever. Anchor to the install dir (DECOMPUTE_HOME) or ~/.decompute so the path never
    depends on the launch cwd. (Dev runs from a writable cwd, which is why this was latent.)"""
    # DECOMPUTE_CONFIG_DIR is the CLI's knob for WHERE config.toml/claude.key live (sdk config.py).
    # Honor it here too so the gateway reads the SAME dir the CLI wrote to — otherwise setting only
    # CONFIG_DIR (without DATA_DIR/HOME) makes the gateway miss claude.mode=claude_subscription and
    # silently fall back to BYOK. DATA_DIR/HOME still win when set (they pin the install dir).
    raw = os.getenv("DECOMPUTE_DATA_DIR") or os.getenv("DECOMPUTE_HOME") or os.getenv("DECOMPUTE_CONFIG_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".decompute"


@dataclass(frozen=True)
class Settings:
    app_name: str = "Decompute Gateway"
    environment: str = os.getenv("DECOMPUTE_ENV", "local").lower()
    host: str = os.getenv("DECOMPUTE_HOST", "127.0.0.1")
    port: int = _int("DECOMPUTE_PORT", 8080)
    data_dir: Path = field(default_factory=_default_data_dir)

    database_url: str = os.getenv(
        "DECOMPUTE_DATABASE_URL",
        os.getenv("DATABASE_URL") or f"sqlite+pysqlite:///{_default_data_dir() / 'decompute.db'}",
    )
    db_pool_size: int = _int("DECOMPUTE_DB_POOL_SIZE", 5)
    db_max_overflow: int = _int("DECOMPUTE_DB_MAX_OVERFLOW", 10)

    require_auth_env: bool = _bool("DECOMPUTE_REQUIRE_AUTH", False)
    dev_api_key: str = os.getenv("DECOMPUTE_API_KEY", "dc_dev_local")
    dashboard_auth_required_env: bool = _bool("DECOMPUTE_DASHBOARD_AUTH_REQUIRED", False)
    allow_client_provider_key: bool = _bool("DECOMPUTE_ALLOW_CLIENT_PROVIDER_KEY", True)
    # Local Claude Pro/Max/Team users authenticate Claude Code with OAuth. In that path the client
    # sends a provider credential as Authorization: Bearer and the local gateway may pass it through
    # to Anthropic. Hosted production gateways must not accept consumer subscription OAuth by default;
    # enable explicitly only for self-managed/enterprise deployments that own the credential flow.
    allow_client_bearer_provider_auth_override: bool | None = _bool_opt("DECOMPUTE_ALLOW_CLIENT_BEARER_PROVIDER_AUTH")
    api_key_hash_salt: str = os.getenv("DECOMPUTE_API_KEY_HASH_SALT", "local-dev-salt-change-me")

    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    anthropic_base_url: str = os.getenv("ANTHROPIC_BASE_URL_UPSTREAM", "https://api.anthropic.com")
    anthropic_version: str = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    allowed_anthropic_beta_headers: list[str] = field(default_factory=lambda: _csv("DECOMPUTE_ALLOWED_ANTHROPIC_BETAS", ""))
    mock_anthropic: bool = _bool("DECOMPUTE_MOCK_ANTHROPIC", False)

    yscompress_base_url: str | None = os.getenv("YSCOMPRESS_BASE_URL") or os.getenv("HEADROOM_BASE_URL")
    yscompress_timeout_s: float = _float("YSCOMPRESS_TIMEOUT_S", _float("HEADROOM_TIMEOUT_S", 8.0))
    yscompress_default_mode: str = os.getenv("YSCOMPRESS_MODE", os.getenv("HEADROOM_MODE", "safe"))
    yscompress_ccr_enabled: bool = _bool("YSCOMPRESS_CCR", _bool("HEADROOM_CCR", False))
    yscompress_bypass_header: str = "x-yscompress-bypass"
    # Compress user-message content (attached files, pasted docs/logs). Default ON: without it the
    # engine skips ALL user-message content (a coding-agent safety default), so a request that's
    # mostly an attached file shows ~0% savings. Large blocks compress (retrievable on demand via
    # CCR); short blocks — the user's actual question — pass through, and code the user asks to
    # "review/analyze" stays protected. Set DECOMPUTE_COMPRESS_USER_MESSAGES=false for max fidelity.
    compress_user_messages: bool = _bool("DECOMPUTE_COMPRESS_USER_MESSAGES", True)

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
    # When the client already manages prompt caching (Claude Code sends cache_control), compressing
    # the conversation rewrites the cached prefix and BUSTS the cache — re-billing the full context
    # at list price every turn. The cache discount (~90% off repeated input) dwarfs compression
    # savings for agentic sessions, so by default we skip compression for cache-controlled requests
    # and let the cache work. Set false to force compression (accepting the cache bust).
    preserve_prompt_cache: bool = _bool("DECOMPUTE_PRESERVE_PROMPT_CACHE", True)
    # ...but only when the request is THIS large (estimated input tokens). Below this, compression
    # wins (new content compresses ~90-99% and any cache being busted is small); above it, the cache
    # discount on a huge repeated context dominates, so we preserve it. Tuned to sit between a normal
    # Claude Code turn / log read (tens of K) and a giant re-sent Opus context (hundreds of K).
    preserve_cache_min_tokens: int = _int("DECOMPUTE_PRESERVE_CACHE_MIN_TOKENS", 250000)

    # Privacy-safe support bundle (decompute support bundle / POST /v1/support/bundle).
    # Defaults the bundle's PII mode to the gateway-wide pii_mode so they stay aligned.
    support_bundle_enabled: bool = _bool("DECOMPUTE_SUPPORT_BUNDLE_ENABLED", True)
    support_bundle_output_dir: Path = Path(os.getenv("DECOMPUTE_SUPPORT_BUNDLE_DIR")).expanduser() if os.getenv("DECOMPUTE_SUPPORT_BUNDLE_DIR") else _default_data_dir() / "support"
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
    billing_success_url: str = os.getenv("DECOMPUTE_BILLING_SUCCESS_URL", "https://decompute.run/billing/success")
    billing_cancel_url: str = os.getenv("DECOMPUTE_BILLING_CANCEL_URL", "https://decompute.run/billing/cancel")
    billing_portal_return_url: str = os.getenv("DECOMPUTE_BILLING_PORTAL_RETURN_URL", "https://decompute.run/account")

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
    # Counted PER /v1/messages request, and Claude Code fires many requests per user prompt (tool
    # calls, sub-agents), so 100 was exhausted in ~1 prompt. 1000/day gives a usable free experience;
    # lower it (env DECOMPUTE_FREE_COMPRESSION_QUOTA) to monetize harder — it's the key pricing knob.
    free_compression_quota: int = _int("DECOMPUTE_FREE_COMPRESSION_QUOTA", 1000)
    free_quota_window: str = os.getenv("DECOMPUTE_FREE_QUOTA_WINDOW", "daily")  # daily | lifetime
    device_license_tier: str = os.getenv("DECOMPUTE_DEVICE_LICENSE_TIER", "pro")
    # Purchase site the upgrade modal/CLI sends the user to, and the app deep-link to return to.
    pro_upgrade_url: str = os.getenv("DECOMPUTE_PRO_UPGRADE_URL", "https://decompute.run/pro")
    pro_upgrade_return_to: str | None = os.getenv("DECOMPUTE_PRO_UPGRADE_RETURN_TO", "decompute://license/activate")
    license_max_devices: int = _int("DECOMPUTE_LICENSE_MAX_DEVICES", 1)
    # Admin/dev license minting (no-Stripe grant) is OFF by default: on a self-hosted gateway the
    # default principal is local_noauth/admin, so an open endpoint would be a free-Pro button.
    # Enable only for manual ops / demos behind real admin auth.
    enable_admin_license_issue: bool = _bool("DECOMPUTE_ENABLE_ADMIN_LICENSE_ISSUE", False)

    # ── Offline signed-license file (local-engine Pro gate) ───────────────────
    # When the gateway runs ON THE USER'S MACHINE the DB row above is forgeable (insert a License
    # row -> free Pro), so Pro is instead proven by an Ed25519-SIGNED license file the hosted issuer
    # mints. The engine ships only the PUBLIC key(s) and verifies offline; see signed_license.py.
    signed_license_enabled: bool = _bool("DECOMPUTE_SIGNED_LICENSE_ENABLED", True)
    # Path to the signed license file (chmod 600). Tilde-expanded at read time.
    license_file: str = os.getenv("DECOMPUTE_LICENSE_FILE", "~/.decompute/license.dcmp")
    # Trusted public verify-keys as JSON {kid: PEM}. Empty => only signed_license.EMBEDDED_*
    # (which a release build bakes in) is trusted. The PRIVATE key NEVER lives here.
    license_public_keys_json: str = os.getenv("DECOMPUTE_LICENSE_PUBLIC_KEYS", "")
    # Forward clock-skew tolerance and post-expiry offline grace (a lapsed sub keeps Pro this long
    # after the signed expiry, since an offline client can't be told the sub ended any sooner).
    license_clock_skew_s: int = _int("DECOMPUTE_LICENSE_CLOCK_SKEW_S", 300)
    license_offline_grace_days: int = _int("DECOMPUTE_LICENSE_OFFLINE_GRACE_DAYS", 0)

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    # ── Anonymous savings telemetry (counts only) ─────────────────────────────
    # Sends a WEEKLY rollup of savings COUNTS (requests, tokens_saved, tokens_before, est_$ saved) to
    # the telemetry Worker so we can see aggregate savings across users. NEVER sends prompts, files,
    # messages, model inputs, or any user content — see telemetry.py, which is the only writer and
    # asserts the payload is counts-only. On by default but trivially disabled; the gateway also honors
    # the DO_NOT_TRACK convention, so a user who exports DO_NOT_TRACK=1 is never beaconed.
    telemetry_enabled: bool = _bool("DECOMPUTE_TELEMETRY", True) and not _bool("DO_NOT_TRACK", False)
    telemetry_url: str = os.getenv("DECOMPUTE_TELEMETRY_URL", "https://decompute-telemetry.xcomp-decompute.workers.dev")
    telemetry_interval_days: int = _int("DECOMPUTE_TELEMETRY_INTERVAL_DAYS", 7)

    # Retry transient upstream failures (500/504/529) with exponential backoff.
    upstream_max_retries: int = _int("DECOMPUTE_UPSTREAM_MAX_RETRIES", 2)
    upstream_retry_backoff_s: float = _float("DECOMPUTE_UPSTREAM_RETRY_BACKOFF_S", 0.25)

    # Upstream HTTP timeouts. A bare timeout=None lets a stalled Anthropic response hang
    # the proxy (and the Claude Code client) forever — and a hung stream never releases its
    # concurrency slot. connect/write are shared; read differs by path:
    #   - streaming uses read as the max GAP BETWEEN chunks (Anthropic pings keep an active
    #     stream well under this), with no overall cap so long generations stay legitimate;
    #   - non-streaming uses upstream_overall_timeout_s as read, since the whole body lands
    #     at once and can legitimately take minutes.
    upstream_connect_timeout_s: float = _float("DECOMPUTE_UPSTREAM_CONNECT_TIMEOUT_S", 10.0)
    upstream_read_timeout_s: float = _float("DECOMPUTE_UPSTREAM_READ_TIMEOUT_S", 120.0)
    upstream_write_timeout_s: float = _float("DECOMPUTE_UPSTREAM_WRITE_TIMEOUT_S", 120.0)
    upstream_overall_timeout_s: float = _float("DECOMPUTE_UPSTREAM_OVERALL_TIMEOUT_S", 600.0)

    allowed_models: list[str] = field(default_factory=lambda: _csv("DECOMPUTE_ALLOWED_MODELS", "claude-sonnet,claude-opus,claude-haiku"))
    return_proxy_metadata: bool = _bool("DECOMPUTE_RETURN_PROXY_METADATA", False)

    yellowstone_policy_version: str = os.getenv("YELLOWSTONE_POLICY_VERSION", "v0.1")
    yellowstone_planner_version: str = os.getenv("YELLOWSTONE_PLANNER_VERSION", "v0.1")
    local_preprocess_enabled: bool = _bool("YELLOWSTONE_LOCAL_PREPROCESS_ENABLED", False)
    local_student_enabled: bool = _bool("DECOMPUTE_LOCAL_STUDENT_ENABLED", False)
    local_student_training_enabled: bool = _bool("DECOMPUTE_LOCAL_STUDENT_TRAINING_ENABLED", False)
    # OPT-IN active serving on the live /v1/messages proxy path (the one `decompute claude` uses). When
    # true AND the master switch is on, an eligible coding turn is first attempted by the local student;
    # the verifier accepts it (answer served locally, no Claude call) or escalates, falling through to
    # the SAME upstream Claude call the request would have made anyway — reusing the request's own
    # subscription/API credential, so no separate teacher auth is needed. OFF by default: with this
    # false the student is observe-only on /v1/messages and serves only via the explicit ts-run command.
    local_student_serve: bool = _bool("DECOMPUTE_LOCAL_STUDENT_SERVE", False)

    # ── Local coding student (teacher-student) ────────────────────────────────
    # An OPTIONAL, off-by-default local model that answers narrow coding tasks. Invoked via the explicit
    # `decompute ts-run`/`ts-health`/`local-chat` commands, and — only when local_student_serve is on —
    # inline on the /v1/messages proxy path (student-first, escalate-to-Claude). The provider speaks
    # OpenAI-compatible chat-completions (llama.cpp `llama-server`, Ollama, LM Studio, vLLM). See
    # docs/teacher_student_plan.md. local_student_enabled (above) is the master switch.
    local_student_backend: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_BACKEND", "llama_cpp")
    local_student_base_url: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_BASE_URL", "http://127.0.0.1:18080/v1")
    local_student_model: str = os.getenv("DECOMPUTE_LOCAL_STUDENT_MODEL", "local-student-coder")
    local_student_api_key: str | None = os.getenv("DECOMPUTE_LOCAL_STUDENT_API_KEY")
    local_student_context_window: int = _int("DECOMPUTE_LOCAL_STUDENT_CONTEXT_WINDOW", 32_000)
    local_student_max_output_tokens: int = _int("DECOMPUTE_LOCAL_STUDENT_MAX_OUTPUT_TOKENS", 2048)
    local_student_connect_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_CONNECT_TIMEOUT_S", 3.0)
    local_student_read_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_READ_TIMEOUT_S", 60.0)
    local_student_overall_timeout_s: float = _float("DECOMPUTE_LOCAL_STUDENT_OVERALL_TIMEOUT_S", 120.0)
    local_student_health_ttl_s: float = _float("DECOMPUTE_LOCAL_STUDENT_HEALTH_TTL_S", 15.0)
    local_student_max_concurrency: int = _int("DECOMPUTE_LOCAL_STUDENT_MAX_CONCURRENCY", 1)
    # A test-ONLY in-process mock provider. NEVER a runtime fallback for a down model — a down student
    # raises a typed error or escalates to the teacher (plan invariant #3, "no silent mock fallback").
    # Rejected in production by validate_for_startup.
    local_student_mock: bool = _bool("DECOMPUTE_LOCAL_STUDENT_MOCK", False)
    # The student is local by default. To point it at a REMOTE endpoint in production you must opt in
    # explicitly (and use https) — see the SSRF guard in validate_for_startup.
    local_student_allow_remote: bool = _bool("DECOMPUTE_LOCAL_STUDENT_ALLOW_REMOTE", False)

    # Teacher-student verifier / governance (consumed in later phases; declared here so production
    # validation can reject unsafe combinations from the first ship).
    # The Claude model used as the teacher when the verifier escalates. Configurable so a retired
    # model id doesn't 404 the teacher path (Orig-1). Passed through the gateway's normal model
    # allowlist + provider, so it must be an allowed Claude model.
    teacher_student_teacher_model: str = os.getenv("DECOMPUTE_TS_TEACHER_MODEL", "claude-sonnet-4-5")
    teacher_student_sandbox_backend: str = os.getenv("DECOMPUTE_TS_SANDBOX_BACKEND", "disabled")
    teacher_student_test_execution_enabled: bool = _bool("DECOMPUTE_TS_TEST_EXECUTION", False)
    # Max concurrent sandbox containers across the process (Orig-4): bounds resource use / DoS.
    teacher_student_sandbox_max_concurrency: int = _int("DECOMPUTE_TS_SANDBOX_MAX_CONCURRENCY", 2)
    # Container-sandbox hardening (used only when backend=container). All have safe ceilings.
    teacher_student_sandbox_runtime: str = os.getenv("DECOMPUTE_TS_SANDBOX_RUNTIME", "docker")
    teacher_student_sandbox_image: str = os.getenv("DECOMPUTE_TS_SANDBOX_IMAGE", "python:3.12-slim")
    teacher_student_sandbox_timeout_s: int = _int("DECOMPUTE_TS_SANDBOX_TIMEOUT_S", 15)
    teacher_student_sandbox_memory: str = os.getenv("DECOMPUTE_TS_SANDBOX_MEMORY", "256m")
    teacher_student_sandbox_cpus: str = os.getenv("DECOMPUTE_TS_SANDBOX_CPUS", "1.0")
    teacher_student_sandbox_pids: int = _int("DECOMPUTE_TS_SANDBOX_PIDS", 64)
    teacher_student_confidence_threshold: float = _float("DECOMPUTE_TS_CONFIDENCE_THRESHOLD", 0.82)
    teacher_student_judge_enabled: bool = _bool("DECOMPUTE_TS_JUDGE_ENABLED", False)
    teacher_student_capture_enabled: bool = _bool("DECOMPUTE_TS_CAPTURE_ENABLED", False)
    teacher_student_governance_mode: str = os.getenv("DECOMPUTE_TS_GOVERNANCE_MODE", "metadata_only")
    teacher_student_capture_retention_days: int = _int("DECOMPUTE_TS_CAPTURE_RETENTION_DAYS", 30)
    # Using Claude (teacher) outputs as model-training targets. Legal/product approved this for the
    # teacher-student distillation work (see CLAUDE.md "Policy exception"), but it stays default-off and
    # production must explicitly encode the sign-off via DECOMPUTE_TS_CLAUDE_TRAINING_SIGNOFF.
    teacher_student_allow_claude_training_targets: bool = _bool("DECOMPUTE_TS_ALLOW_CLAUDE_TRAINING_TARGETS", False)
    teacher_student_claude_training_signoff: bool = _bool("DECOMPUTE_TS_CLAUDE_TRAINING_SIGNOFF", False)

    @property
    def local_student_base_url_is_loopback(self) -> bool:
        """True if the configured student endpoint is on the local machine. The student is a LOCAL
        model; a non-loopback http:// URL in production would mean shipping plaintext prompts to a
        remote host, which validate_for_startup rejects."""
        from urllib.parse import urlparse

        host = (urlparse(self.local_student_base_url).hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

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
    def allow_client_bearer_provider_auth(self) -> bool:
        if self.allow_client_bearer_provider_auth_override is not None:
            return self.allow_client_bearer_provider_auth_override
        return not self.is_production

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            rel = self.database_url.split("///", 1)[-1]
            db_path = Path(rel)
            if not db_path.is_absolute():
                # Never anchor the DB to the (read-only under launchd) cwd — resolve under data_dir.
                db_path = self.data_dir / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)

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
            if self.billing_enabled and not self.stripe_webhook_secret:
                # Without it the webhook 400s every event, so paid checkouts never grant a tier —
                # customers pay and get nothing. Fail closed rather than ship a silent dead paywall.
                errors.append("DECOMPUTE_STRIPE_WEBHOOK_SECRET must be set when billing is enabled in production")
            if not self.dashboard_auth_required:
                errors.append("DECOMPUTE_DASHBOARD_AUTH_REQUIRED must be true in production")
            if self.local_student_training_enabled:
                errors.append("Local student continuous training is not a V0 production feature")
            # ── Teacher-student production safety (plan §"Production validation") ──
            # No silent mock answer can ever ship: the test-only in-process mock is a dev convenience,
            # and in production it would return a canned answer as if it came from a real model.
            if self.local_student_mock:
                errors.append("DECOMPUTE_LOCAL_STUDENT_MOCK must be false in production (no mock answers)")
            # SSRF / exfiltration guard (H3): the "local" student must be LOOPBACK in production unless
            # an operator explicitly opts into a remote endpoint. Without the opt-in, ANY non-loopback
            # host (even https) is rejected — otherwise a misconfigured base_url could ship every prompt
            # to an arbitrary internal/external host. With the opt-in, the remote endpoint must be https.
            if self.local_student_enabled and not self.local_student_base_url_is_loopback:
                if not self.local_student_allow_remote:
                    errors.append(
                        "DECOMPUTE_LOCAL_STUDENT_BASE_URL must be loopback in production. To use a remote "
                        "student set DECOMPUTE_LOCAL_STUDENT_ALLOW_REMOTE=true (and use https)."
                    )
                elif not self.local_student_base_url.startswith("https://"):
                    errors.append(
                        "A remote DECOMPUTE_LOCAL_STUDENT_BASE_URL must use https in production "
                        "(plaintext prompts would cross the network)."
                    )
            # Test execution must run in a real sandbox, never bare subprocess on the request path.
            if self.teacher_student_test_execution_enabled and self.teacher_student_sandbox_backend == "disabled":
                errors.append(
                    "DECOMPUTE_TS_TEST_EXECUTION requires a production-safe DECOMPUTE_TS_SANDBOX_BACKEND "
                    "(not 'disabled') in production"
                )
            # Only known, production-safe sandbox backends may execute tests. 'container' (docker/podman)
            # and 'cloudflare' (hosted) are the safe options; anything else (and an unsafe runtime) is rejected.
            if self.teacher_student_test_execution_enabled:
                if self.teacher_student_sandbox_backend not in {"container", "cloudflare"}:
                    errors.append(
                        f"DECOMPUTE_TS_SANDBOX_BACKEND={self.teacher_student_sandbox_backend!r} is not a "
                        "production-safe sandbox; use 'container' or 'cloudflare'"
                    )
                if self.teacher_student_sandbox_backend == "container" and self.teacher_student_sandbox_runtime not in {"docker", "podman"}:
                    errors.append(
                        "DECOMPUTE_TS_SANDBOX_RUNTIME must be 'docker' or 'podman' for the container sandbox in production"
                    )
            # Serving the student inline on /v1/messages (DECOMPUTE_LOCAL_STUDENT_SERVE) requires the
            # master switch AND a real verification signal — otherwise a serve would emit an UNVERIFIED
            # answer (the no-evidence cap, 0.55, never clears the threshold, but we refuse to even risk a
            # misconfiguration that serves unchecked). Acceptable signals: an independent judge, or test
            # execution in a production-safe sandbox. The judge is the expected signal on the proxy path
            # (real requests carry no tests).
            if self.local_student_serve:
                if not self.local_student_enabled:
                    errors.append("DECOMPUTE_LOCAL_STUDENT_SERVE requires DECOMPUTE_LOCAL_STUDENT_ENABLED=true")
                has_judge = self.teacher_student_judge_enabled
                has_sandbox = self.teacher_student_test_execution_enabled and self.teacher_student_sandbox_backend in {"container", "cloudflare"}
                if not (has_judge or has_sandbox):
                    errors.append(
                        "DECOMPUTE_LOCAL_STUDENT_SERVE requires a verification signal: set "
                        "DECOMPUTE_TS_JUDGE_ENABLED=true (recommended for the proxy path) or enable test "
                        "execution with a production-safe sandbox. Serving without verification is refused."
                    )
            # Distillation on Claude outputs is approved but governed: production must explicitly encode
            # the legal/product sign-off, not just flip the allow flag (CLAUDE.md "Policy exception").
            if self.teacher_student_allow_claude_training_targets and not self.teacher_student_claude_training_signoff:
                errors.append(
                    "DECOMPUTE_TS_ALLOW_CLAUDE_TRAINING_TARGETS requires DECOMPUTE_TS_CLAUDE_TRAINING_SIGNOFF "
                    "to be set in production (records the legal/product approval)"
                )
            # Capture must never store raw content without a redaction mode and CCR encryption configured,
            # mirroring the existing CCR production gate.
            if (self.teacher_student_capture_enabled
                    and self.teacher_student_governance_mode not in {"metadata_only", "redacted", "regulated"}):
                errors.append(
                    "DECOMPUTE_TS_GOVERNANCE_MODE must be one of metadata_only|redacted|regulated when capture is enabled"
                )
            if (self.teacher_student_capture_enabled
                    and self.teacher_student_governance_mode == "redacted"
                    and not self.ccr_encryption_key):
                errors.append(
                    "DECOMPUTE_CCR_ENCRYPTION_KEY is required when teacher-student capture stores redacted content"
                )
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
