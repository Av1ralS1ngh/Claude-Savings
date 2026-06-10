from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any

# Local config store.
#  - config.toml : NON-SECRET settings (gateway URL, privacy modes, CCR, claude mode, license cache).
#  - claude.key  : the user's own Anthropic/Claude key (BYOK), owner-only (chmod 600).
#  - device.id   : a stable, locally-generated device id (NO hardware identifiers) — the
#                  licensing unit. Sent (raw) as the x-decompute-device header; the gateway
#                  HMAC-hashes it server-side.
#  - license.key : the device's Pro license key (secret, chmod 600), if activated.
# Decompute issues no API key of its own — you bring your Claude key.
CONFIG_DIR = Path(os.getenv("DECOMPUTE_CONFIG_DIR", str(Path.home() / ".decompute")))
CONFIG_TOML = CONFIG_DIR / "config.toml"
KEY_FILE = CONFIG_DIR / "claude.key"
DEVICE_ID_FILE = CONFIG_DIR / "device.id"
LICENSE_FILE = CONFIG_DIR / "license.key"
LEGACY_JSON = CONFIG_DIR / "config.json"
CONFIG_PATH = CONFIG_TOML  # back-compat alias for callers that print the path

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# Secret hygiene. The gateway is the authority on entitlements (it HMAC-hashes the device and
# grants tier only from a signature-verified Stripe webhook), so these files are credential
# CACHES, not a security boundary — the goal is owner-only, never-half-written, never-leaked.
_DIR_MODE = 0o700     # owner-only dir: secret *filenames* aren't even world-listable
_SECRET_MODE = 0o600  # owner-only files


def _ensure_config_dir() -> Path:
    """Create ~/.decompute owner-only, tightening perms even if it pre-existed looser."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(_DIR_MODE)
    except Exception:
        pass
    return CONFIG_DIR


def _write_secret(path: Path, value: str) -> Path:
    """Atomically write a secret owner-only. mkstemp creates the temp file 0600 from the first
    byte (no world-readable window the old write-then-chmod left), fsync + os.replace swap it in
    so a crash never leaves a partial or default-perm secret on disk."""
    _ensure_config_dir()
    data = (value.strip() + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, _SECRET_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)  # no-op (already closed by fdopen) on most paths; safe if fchmod failed
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _valid_secret(value: str | None) -> bool:
    """Reject input that is obviously NOT a credential (empty, internal whitespace, control
    chars, absurd length) — e.g. a pasted error message. The gateway validates real validity;
    this only stops a malformed value from being persisted/sent. Deliberately lenient so a
    well-formed server-issued key is never rejected."""
    k = (value or "").strip()
    if not (1 <= len(k) <= 256):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in k):
        return False
    return not any(c.isspace() for c in k)

DEFAULTS: dict[str, Any] = {
    "gateway": {"base_url": DEFAULT_BASE_URL, "auto_start": True},
    "privacy": {"pii_mode": "tokenize", "secret_mode": "redact"},
    "ccr": {"enabled": True, "retention_days": 7},
    "claude": {"mode": "anthropic_api"},  # anthropic_api (BYOK) | enterprise_gateway
}


def _deep_merge(base: dict, over: dict | None) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_toml() -> dict:
    try:
        return tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _migrate_legacy() -> dict:
    """One-time best-effort pull of non-secret settings from the old config.json."""
    try:
        j = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, Any] = {}
    if j.get("base_url"):
        out["gateway"] = {"base_url": str(j["base_url"]).rstrip("/")}
    if j.get("last_session_id"):
        out["claude"] = {"last_session_id": j["last_session_id"]}
    return out


def load_config() -> dict:
    """Full non-secret config: defaults merged with config.toml (or migrated legacy json)."""
    cfg = _read_toml()
    if not cfg and LEGACY_JSON.exists():
        cfg = _migrate_legacy()
    return _deep_merge(DEFAULTS, cfg)


def _render_toml(cfg: dict) -> str:
    lines: list[str] = []
    for section in ("gateway", "privacy", "ccr", "claude", "license"):
        sec = cfg.get(section) or {}
        if not sec:
            continue
        lines.append(f"[{section}]")
        for k, v in sec.items():
            if isinstance(v, bool):
                rendered = "true" if v else "false"
            elif isinstance(v, (int, float)):
                rendered = str(v)
            else:
                if v in (None, ""):
                    continue  # omit empty optionals (last_session_id, upstream_base_url)
                rendered = '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append(f"{k} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_config(updates: dict) -> Path:
    """Deep-merge nested updates into config.toml. NEVER write the Claude key here.

    Written atomically (temp file in the same dir → fsync → os.replace) so a concurrent reader
    (the gateway, or the VS Code extension polling for config.toml during `decompute init`) never
    sees a truncated/half-written file: config.toml holds either the old or the complete new
    contents, never a partial state."""
    _ensure_config_dir()  # owner-only dir, even if it pre-existed looser
    data = _render_toml(_deep_merge(load_config(), updates or {})).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=CONFIG_TOML.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_TOML)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return CONFIG_TOML


# ── convenience accessors ─────────────────────────────────────────────────────
def base_url() -> str:
    return (load_config().get("gateway", {}).get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def get_session_id() -> str | None:
    return load_config().get("claude", {}).get("last_session_id") or None


def set_session_id(sid: str) -> None:
    save_config({"claude": {"last_session_id": sid}})


def load_claude_key() -> str | None:
    """BYOK key resolution: explicit env first, then the on-disk key file."""
    env = os.getenv("DECOMPUTE_CLAUDE_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        return (KEY_FILE.read_text(encoding="utf-8").strip() or None)
    except Exception:
        return None


def save_claude_key(key: str) -> Path:
    if not _valid_secret(key):
        raise ValueError("refusing to save a malformed Claude key")
    return _write_secret(KEY_FILE, key)  # atomic, owner-only (0600), no world-readable window


def clear_claude_key() -> None:
    try:
        KEY_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── device identity + license (BYOK device-licensing axis) ────────────────────
def load_device_id() -> str:
    """Stable per-device id, minted once and persisted. Privacy-respecting: a random UUID,
    NOT a hardware identifier — it leaks nothing about the host. Sent raw as x-decompute-device;
    the gateway HMAC-hashes it. Env override (DECOMPUTE_DEVICE_ID) is honored for CI/containers."""
    env = os.getenv("DECOMPUTE_DEVICE_ID")
    if env:
        return env.strip()
    try:
        existing = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except Exception:
        pass
    device_id = "dev-" + uuid.uuid4().hex
    try:
        _write_secret(DEVICE_ID_FILE, device_id)  # atomic, owner-only — the licensing unit
    except Exception:
        pass
    return device_id


def load_license_key() -> str | None:
    env = os.getenv("DECOMPUTE_LICENSE_KEY")
    if env:
        return env.strip()
    try:
        return (LICENSE_FILE.read_text(encoding="utf-8").strip() or None)
    except Exception:
        return None


def save_license_key(key: str) -> Path:
    if not _valid_secret(key):
        raise ValueError("refusing to save a malformed license key")
    return _write_secret(LICENSE_FILE, key)  # atomic, owner-only (0600)


def clear_license_key() -> None:
    try:
        LICENSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
