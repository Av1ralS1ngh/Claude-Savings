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
import sys
from pathlib import Path


def _port_free(host: str, port: int) -> bool:
    """True if we could bind host:port. Uses SO_REUSEADDR to match uvicorn's bind, so a TIME_WAIT
    socket from our own just-exited process doesn't read as 'in use' (which would loop under launchd)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _gateway_alive(host: str, port: int) -> bool:
    """True if a Decompute gateway is already answering on host:port (so we reuse it, not error)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


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


def _prewarm_engine() -> None:
    """Load the compression model from the local cache at startup (best-effort, in a daemon thread),
    so the FIRST real request isn't delayed by the ~3s ONNX model load. Runs under HF_HUB_OFFLINE, so
    it never blocks on the network; any failure just means the first request loads it (or the engine
    falls back to non-ML compression). The warmed model lives in a module-global cache the app reuses."""
    try:
        from headroom.transforms.kompress_compressor import _load_kompress
        _load_kompress()
    except Exception as exc:  # best-effort; surface why so a silent skip is debuggable
        import sys
        print(f"[decompute] model pre-warm skipped: {type(exc).__name__}: {exc}", file=sys.stderr)


def main() -> None:
    _load_env()  # must precede the app import so config sees the env
    # Packaged installs are BYOK/real-upstream by default. Set this before config.py calls
    # load_dotenv(override=False), so a workspace .env from the repo cannot accidentally
    # force the console-script launcher back into canned mock responses.
    os.environ.setdefault("DECOMPUTE_MOCK_ANTHROPIC", "false")
    host = os.getenv("DECOMPUTE_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("DECOMPUTE_PORT", "8080"))
    except ValueError:
        port = 8080

    # If the port is already taken, don't fight it with a cryptic bind traceback: reuse our own gateway
    # if that's what's there, otherwise exit with a clear, actionable message.
    if not _port_free(host, port):
        if _gateway_alive(host, port):
            print(f"[decompute] gateway already running on {host}:{port} — nothing to do.", file=sys.stderr)
            return
        print(f"[decompute] error: port {port} is in use by another process.", file=sys.stderr)
        print("[decompute]   Set DECOMPUTE_PORT to a free port in ~/.decompute/gateway.env, then re-run"
              " `decompute configure claude-code` (and reload Claude Code).", file=sys.stderr)
        raise SystemExit(1)

    # Offline-first ML cache by default (no silent network fetch that degrades compression quality).
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    # Warm the compression model from cache BEFORE serving, so the first request is fast (the ONNX
    # model load is ~3s on first use; compression runs in the default thread executor, so this shared
    # in-process cache is reused). Best-effort + offline → can't hang; on a fresh machine the load
    # fails fast and the gateway still starts (compression falls back to non-ML).
    _prewarm_engine()

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
