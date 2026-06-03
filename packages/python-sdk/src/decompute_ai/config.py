from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

# Local config store.
#  - config.toml : NON-SECRET settings (gateway URL, privacy modes, CCR, claude mode).
#  - claude.key  : the user's own Anthropic/Claude key (BYOK), owner-only (chmod 600).
# Decompute issues no API key of its own — you bring your Claude key.
CONFIG_DIR = Path(os.getenv("DECOMPUTE_CONFIG_DIR", str(Path.home() / ".decompute")))
CONFIG_TOML = CONFIG_DIR / "config.toml"
KEY_FILE = CONFIG_DIR / "claude.key"
LEGACY_JSON = CONFIG_DIR / "config.json"
CONFIG_PATH = CONFIG_TOML  # back-compat alias for callers that print the path

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

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
    for section in ("gateway", "privacy", "ccr", "claude"):
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
    """Deep-merge nested updates into config.toml. NEVER write the Claude key here."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_TOML.write_text(_render_toml(_deep_merge(load_config(), updates or {})), encoding="utf-8")
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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        KEY_FILE.chmod(0o600)  # the only real secret on disk — owner-only
    except Exception:
        pass
    return KEY_FILE


def clear_claude_key() -> None:
    try:
        KEY_FILE.unlink(missing_ok=True)
    except Exception:
        pass
