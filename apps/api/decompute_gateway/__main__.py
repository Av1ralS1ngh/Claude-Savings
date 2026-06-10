from __future__ import annotations

"""`decompute-gateway` entry point — starts the local Decompute gateway on 127.0.0.1:8080.

`decompute claude` discovers this command via shutil.which("decompute-gateway") (see the SDK's
_find_gateway_launcher) and launches it. This replaces the dev-only scripts/run_gateway.sh, which
hardcodes an absolute interpreter path and binds the repo working tree — neither works once the
gateway is pip-installed on a user's machine.

It loads ~/.decompute/gateway.env (then ~/.decompute/.env) BEFORE importing the app, so config.py —
which reads os.getenv at import time — picks up settings like DECOMPUTE_LICENSE_PUBLIC_KEYS that the
installer writes. Production defaults: loopback only, single worker, no --reload.
"""

import os
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for p in (Path.home() / ".decompute" / "gateway.env", Path.home() / ".decompute" / ".env"):
        try:
            if p.exists():
                load_dotenv(p, override=False)
        except OSError:
            pass


def main() -> None:
    _load_env()  # must precede the app import so config sees the env
    host = os.getenv("DECOMPUTE_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("DECOMPUTE_PORT", "8080"))
    except ValueError:
        port = 8080
    # Offline-first ML cache by default (no silent network fetch that degrades compression quality).
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import uvicorn

    # Import string (not the app object) so uvicorn imports the app AFTER env is loaded.
    uvicorn.run(
        "decompute_gateway.main:app",
        host=host,
        port=port,
        log_level=os.getenv("DECOMPUTE_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
