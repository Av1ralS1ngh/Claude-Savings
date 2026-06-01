from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Local credential/config store written by `decompute login` / `decompute config-set`.
# Keep it thin: just connection settings. No provider secrets, no core algorithms.
CONFIG_DIR = Path(os.getenv("DECOMPUTE_CONFIG_DIR", str(Path.home() / ".decompute")))
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(values: dict[str, Any]) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = load_config()
    current.update({k: v for k, v in values.items() if v is not None})
    CONFIG_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)  # credentials — owner-only
    except Exception:
        pass
    return CONFIG_PATH
