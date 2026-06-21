from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import typer
from rich import print
from rich.table import Table

from .client import Decompute
from .config import (
    CONFIG_DIR,
    CONFIG_PATH,
    CONFIG_TOML,
    DEFAULT_BASE_URL,
    DEVICE_ID_FILE,
    KEY_FILE,
    LEGACY_JSON,
    LICENSE_FILE,
    base_url as cfg_base_url,
    clear_claude_key,
    clear_license_key,
    get_session_id,
    load_claude_key,
    load_config,
    load_device_id,
    load_license_key,
    save_claude_key,
    save_config,
    save_license_key,
    set_session_id,
)
from .errors import DecomputeError

app = typer.Typer(help="Decompute Gateway CLI — BYOK (bring your own Claude key)")
configure_app = typer.Typer(help="Configure editor and agent integrations.")
app.add_typer(configure_app, name="configure")

HOSTED_GATEWAY_URL = "https://gateway.decompute.run"
CLAUDE_CONFIG_DIR = Path(os.getenv("DECOMPUTE_CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
CLAUDE_SETTINGS = CLAUDE_CONFIG_DIR / "settings.json"
AUTH_API_KEY = "anthropic_api"
AUTH_SUBSCRIPTION = "claude_subscription"
AUTH_ENTERPRISE = "enterprise_gateway"
AUTH_MODES = {AUTH_API_KEY, AUTH_SUBSCRIPTION, AUTH_ENTERPRISE}


def _client() -> Decompute:
    return Decompute()


@app.command()
def health():
    print(_client().get("/health"))


def _set_dotted(updates: dict, dotted: str, value) -> dict:
    """Build a nested update dict from a 'section.key' path, e.g. 'gateway.base_url'."""
    parts = dotted.split(".")
    node = updates
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return updates


def _coerce(value: str):
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if value.isdigit():
        return int(value)
    return value


@app.command("config-set")
def config_set(key: str, value: str):
    """Set a config value by dotted path, e.g. `decompute config-set gateway.base_url http://host:8080`."""
    if "." not in key:
        print("[yellow]Use a dotted path:[/yellow] section.key  (gateway|privacy|ccr|claude).<name>")
        raise typer.Exit(1)
    path = save_config(_set_dotted({}, key, _coerce(value)))
    print(f"[green]✓[/green] {key} = {value}  ({path})")


@app.command("config-show")
def config_show():
    """Print the non-secret config (config.toml). The Claude key is never shown."""
    cfg = load_config()
    cfg.setdefault("_meta", {})["claude_key_set"] = bool(load_claude_key())
    print(json.dumps(cfg, indent=2))


def _conn() -> tuple[str, str]:
    """(base_url, claude_key) — base from config.toml, key from the BYOK secret file (may be '')."""
    return cfg_base_url(), (load_claude_key() or "")


def _claude_mode(cfg: dict | None = None) -> str:
    mode = ((cfg or load_config()).get("claude", {}).get("mode") or AUTH_API_KEY).strip()
    return mode if mode in AUTH_MODES else AUTH_API_KEY


def _normalize_auth_mode(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower().replace("-", "_")
    aliases = {
        "api": AUTH_API_KEY,
        "api_key": AUTH_API_KEY,
        "anthropic": AUTH_API_KEY,
        "anthropic_api": AUTH_API_KEY,
        "byok": AUTH_API_KEY,
        "plan": AUTH_SUBSCRIPTION,
        "subscription": AUTH_SUBSCRIPTION,
        "claude_plan": AUTH_SUBSCRIPTION,
        "claude_subscription": AUTH_SUBSCRIPTION,
        "pro": AUTH_SUBSCRIPTION,
        "max": AUTH_SUBSCRIPTION,
        "enterprise": AUTH_ENTERPRISE,
        "enterprise_gateway": AUTH_ENTERPRISE,
        "gateway": AUTH_ENTERPRISE,
    }
    return aliases.get(v)


def _new_session_id() -> str:
    """A fresh, opaque working-session id (safe charset, well under the gateway's 64-char cap)."""
    return f"cc-{uuid.uuid4().hex[:20]}"


def _safe_gateway_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    if not (url.startswith("https://") or url.startswith("http://")):
        raise typer.BadParameter("gateway URL must start with http:// or https://")
    return url


def _is_loopback_gateway(value: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = urlparse(value).hostname or ""
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{path} is not valid JSON; leaving it untouched ({exc})") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"{path} must contain a JSON object; leaving it untouched")
    return obj


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except Exception:
            pass
    tmp = path.with_name(f".{path.name}.decompute.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    try:
        tmp.chmod(mode)
    except Exception:
        pass
    os.replace(tmp, path)


def _claude_code_env(base_url: str, *, decompute_api_key: str | None = None, session_id: str | None = None) -> dict[str, str]:
    sid = session_id or _new_session_id()
    headers = [
        f"x-decompute-session-id: {sid}",
        f"x-decompute-device: {load_device_id()}",
    ]
    if decompute_api_key:
        headers.insert(0, f"x-decompute-api-key: {decompute_api_key.strip()}")
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(headers),
    }


def _write_claude_code_settings(env_block: dict[str, str]) -> None:
    settings = _read_json_object(CLAUDE_SETTINGS)
    env = settings.get("env")
    if env is not None and not isinstance(env, dict):
        raise RuntimeError(f"{CLAUDE_SETTINGS} has a non-object env block; leaving it untouched")
    settings["env"] = {**(env or {}), **env_block}
    _write_json_atomic(CLAUDE_SETTINGS, settings)


@configure_app.command("claude-code")
def configure_claude_code(
    gateway_url: Optional[str] = typer.Option(None, "--gateway-url", help="Decompute Gateway URL. Defaults to hosted, or local for Claude-plan auth."),
    auth_mode: Optional[str] = typer.Option(None, "--auth-mode", help="Claude auth mode: claude_subscription or api_key."),
    local: bool = typer.Option(False, "--local", help="Use the locally configured gateway URL instead of the hosted gateway."),
    decompute_api_key: Optional[str] = typer.Option(None, "--decompute-api-key", envvar="DECOMPUTE_API_KEY", help="Optional Decompute platform key for hosted gateways."),
    print_env: bool = typer.Option(False, "--print-env", help="Print the Claude Code env block instead of writing settings."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without an interactive confirmation."),
):
    """Configure Claude Code to use Decompute Gateway.

    This command belongs in the CLI/native installer path, not in the VS Code extension. It writes
    gateway routing and Decompute headers only; it never asks for provider credentials.
    """
    mode = _normalize_auth_mode(auth_mode)
    if auth_mode and not mode:
        print("[red]✗[/red] --auth-mode must be one of: claude_subscription, api_key")
        raise typer.Exit(2)
    # When --auth-mode is omitted, inherit the user's already-configured mode. Without this, a
    # subscription user re-running `configure claude-code` (e.g. to refresh the session header) would
    # fall through to the HOSTED gateway default below — which blocks bearer passthrough — silently
    # breaking their local Claude-plan routing.
    if not mode:
        mode = _claude_mode()
    if mode == AUTH_ENTERPRISE:
        mode = AUTH_API_KEY
    base_url = _safe_gateway_url(gateway_url or (cfg_base_url() if local or mode == AUTH_SUBSCRIPTION else HOSTED_GATEWAY_URL))
    if mode == AUTH_SUBSCRIPTION and not _is_loopback_gateway(base_url):
        print("[red]✗[/red] Existing Claude plan auth is local-only. Use a local gateway URL, or choose --auth-mode api_key.")
        raise typer.Exit(2)
    sid = _new_session_id()
    env_block = _claude_code_env(base_url, decompute_api_key=decompute_api_key, session_id=sid)

    if print_env:
        for key, value in env_block.items():
            print(f'export {key}="{value}"')
        return

    if not yes:
        print(f"Claude Code settings: [cyan]{CLAUDE_SETTINGS}[/cyan]")
        print(f"Gateway: [cyan]{base_url}[/cyan]")
        if not typer.confirm("Apply this Decompute configuration?", default=True):
            raise typer.Exit(1)

    try:
        _write_claude_code_settings(env_block)
    except RuntimeError as exc:
        print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)

    cfg_update: dict[str, Any] = {"gateway": {"base_url": base_url}}
    if mode:
        cfg_update["claude"] = {"mode": mode}
    save_config(cfg_update)
    set_session_id(sid)
    print(f"[green]✓[/green] Claude Code configured for Decompute Gateway: [cyan]{base_url}[/cyan]")
    if mode == AUTH_SUBSCRIPTION:
        print("[green]✓[/green] Claude auth: existing Claude plan (no API key stored by Decompute)")
        if load_claude_key():
            print("[yellow]⚠[/yellow] Existing API key credentials will be ignored in subscription mode.")
    elif mode == AUTH_API_KEY:
        print("[green]✓[/green] Claude auth: API key")
    print(f"  Settings: [cyan]{CLAUDE_SETTINGS}[/cyan]")
    print("  Restart Claude Code or open a new session for the change to take effect.")


def _claude_env_lines(base_url: str, claude_key: str, session_id: str, *, include_key: bool = True) -> dict[str, str]:
    """The env Claude Code needs: gateway routing, the BYOK Claude key, and the session header.

    ANTHROPIC_CUSTOM_HEADERS makes Claude Code stamp these on every upstream request (main model +
    background + token-count): x-decompute-session-id (groups a run for support bundles) and
    x-decompute-device (the licensing identity the gateway HMAC-hashes for Pro/quota). Multiple
    headers are newline-separated per Claude Code's env reference.
    ANTHROPIC_API_KEY is the user's own Claude key — the gateway forwards it upstream (BYOK).
    Set include_key=False when writing to a file that might be committed (e.g. .vscode/settings.json).
    """
    custom_headers = f"x-decompute-session-id: {session_id}\nx-decompute-device: {load_device_id()}"
    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
    }
    if include_key and claude_key:
        env["ANTHROPIC_API_KEY"] = claude_key
    return env


def _write_claude_env(base_url: str, claude_key: str, session_id: str) -> Path:
    """Write the sourceable Claude Code env file (opt-in persistent path). Returns its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = CONFIG_DIR / "claude.env"
    env_path.write_text(
        "".join(f'export {k}="{v}"\n' for k, v in _claude_env_lines(base_url, claude_key, session_id).items()),
        encoding="utf-8",
    )
    try:
        env_path.chmod(0o600)
    except Exception:
        pass
    return env_path


# ── gateway lifecycle helpers (used by `claude` and `init`) ────────────────────
def _gateway_running(base_url: str, *, timeout: float = 2.0) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url}/health", timeout=timeout).status_code == 200
    except Exception:
        return False


_IS_WINDOWS = sys.platform == "win32"
# The console-script name differs by OS: pip writes decompute-gateway.exe into the venv Scripts dir on
# Windows, decompute-gateway into venv/bin on POSIX. shutil.which resolves both (it honors PATHEXT on
# Windows), but the PATH-independent dir probe below must know the exact basename + subdir.
_GATEWAY_EXE = "decompute-gateway.exe" if _IS_WINDOWS else "decompute-gateway"
_VENV_BINDIR = "Scripts" if _IS_WINDOWS else "bin"


def _find_gateway_launcher() -> Optional[list[str]]:
    """Locate a way to start the gateway: $DECOMPUTE_GATEWAY_CMD, the installed console-script, or
    (dev only, POSIX) scripts/run_gateway.sh. Cross-OS: the venv bin dir is `Scripts` on Windows and the
    entry point is decompute-gateway.exe there."""
    env_cmd = os.getenv("DECOMPUTE_GATEWAY_CMD")
    if env_cmd:
        import shlex
        # shlex.split with POSIX rules mangles Windows backslashes; use the platform-native lexer.
        return shlex.split(env_cmd, posix=not _IS_WINDOWS)
    binary = shutil.which("decompute-gateway")  # honors PATHEXT on Windows -> finds the .exe
    if binary:
        return [binary]
    # PATH-independent fallback: `decompute` and `decompute-gateway` are pip console-scripts installed
    # side-by-side in the SAME venv bin/Scripts dir. If the caller's PATH lacks the install dir (e.g. an
    # absolute-path invocation or the VS Code wizard), `which` misses the gateway even though it exists
    # next to us. Resolve the invoked CLI and check its sibling + the installer's known dirs.
    cli = shutil.which("decompute") or sys.argv[0]
    home = Path.home()
    dchome = Path(os.getenv("DECOMPUTE_HOME") or (home / ".decompute"))
    candidates = [
        Path(cli).resolve().parent / _GATEWAY_EXE,            # next to the resolved CLI (the venv bin/Scripts)
        dchome / "venv" / _VENV_BINDIR / _GATEWAY_EXE,        # installer venv default
    ]
    if not _IS_WINDOWS:
        # POSIX-only: the installer also links a shim into ~/.local/bin (DECOMPUTE_BIN default). On
        # Windows there's no shim dir — the venv Scripts dir goes on PATH directly.
        candidates.insert(1, home / ".local" / "bin" / _GATEWAY_EXE)
    seen: set[Path] = set()
    for cand in candidates:
        try:
            rp = cand.resolve()
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if cand.exists():
            return [str(cand)]
    # Dev fallback (POSIX only): walk up from CWD looking for scripts/run_gateway.sh (a bash script).
    # On Windows there is no run_gateway.sh equivalent; rely on the installed decompute-gateway.exe.
    if not _IS_WINDOWS:
        for base in (Path.cwd(), *Path.cwd().parents):
            cand = base / "scripts" / "run_gateway.sh"
            if cand.exists():
                return ["bash", str(cand)]
    return None


def _start_gateway(base_url: str, *, wait_s: float = 25.0) -> bool:
    """Best-effort: start the gateway detached and poll /health until it is up."""
    launcher = _find_gateway_launcher()
    if not launcher:
        print("[yellow]⚠[/yellow] Could not find a way to start the gateway.")
        print("  Set [bold]DECOMPUTE_GATEWAY_CMD[/bold], or start it yourself (e.g. [bold]scripts/run_gateway.sh[/bold]).")
        return False
    log_path = CONFIG_DIR / "gateway.log"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[dim]Starting gateway: {' '.join(launcher)}  (logs → {log_path})[/dim]")
    # Detach the gateway so it outlives this CLI process. POSIX: start_new_session puts it in its own
    # session (no controlling terminal). Windows has no sessions — use DETACHED_PROCESS (no console) +
    # CREATE_NEW_PROCESS_GROUP (survives this console's Ctrl-C / close), the equivalent idiom.
    popen_kwargs: dict[str, Any] = {"stdout": None, "stderr": None, "stdin": subprocess.DEVNULL}
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        with open(log_path, "ab") as logf:
            popen_kwargs["stdout"] = logf
            popen_kwargs["stderr"] = logf
            subprocess.Popen(launcher, **popen_kwargs)
    except Exception as exc:
        print(f"[red]✗[/red] failed to launch the gateway: {exc}")
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _gateway_running(base_url):
            return True
        time.sleep(0.5)
    print(f"[yellow]⚠[/yellow] gateway didn't answer /health within {wait_s:.0f}s — check {log_path}.")
    return False


_PII_MODES = {1: ("tokenize", "redact"), 2: ("redact", "redact"), 3: ("passthrough", "redact")}


@app.command()
def init(
    yes: bool = typer.Option(False, "--yes", "-y", help="non-interactive — accept all defaults (recommended)."),
    claude_key: Optional[str] = typer.Option(None, "--claude-key", envvar="DECOMPUTE_CLAUDE_KEY", help="Your Anthropic/Claude key (sk-ant-…); stored as a secret."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url", help="Decompute gateway URL."),
    auth_mode: Optional[str] = typer.Option(None, "--auth-mode", help="Claude auth mode: claude_subscription, api_key, or enterprise_gateway."),
):
    """Set up Decompute for Claude Code.

    Subscription mode uses Claude Code's own Claude plan login and stores no Claude credential.
    API-key mode stores your Anthropic key in ~/.decompute/claude.key (chmod 600).
    """
    print("[bold]Decompute setup[/bold] — routes Claude Code through the local compression gateway.\n")

    # 1. Upstream Claude mode.
    mode = _normalize_auth_mode(auth_mode)
    if auth_mode and not mode:
        print("[red]✗[/red] --auth-mode must be one of: claude_subscription, api_key, enterprise_gateway")
        raise typer.Exit(2)
    # Never silently flip an already-configured user's auth mode when they re-run init (e.g. just to
    # change the gateway URL). An EXISTING mode is the default; only a brand-new install with no
    # config falls back to the recommended onboarding default.
    existing_mode = _claude_mode() if (CONFIG_TOML.exists() or LEGACY_JSON.exists()) else None
    if not mode:
        if existing_mode:
            mode = existing_mode                       # preserve the user's current mode by default
        else:
            mode = AUTH_API_KEY if yes else AUTH_SUBSCRIPTION  # fresh install: onboarding default
    upstream_base_url = ""
    if not yes:
        print("[dim]Tip: at each step, press [bold]Enter[/bold] to accept the recommended option shown in [brackets].[/dim]\n")
        if not auth_mode:
            # Pre-select the current/recommended option so pressing Enter keeps an existing user's mode.
            default_choice = {AUTH_API_KEY: "2", AUTH_ENTERPRISE: "3"}.get(mode, "1")
            print("How should Claude Code connect?")
            print("  [1] Use my existing Claude plan   [dim](Pro, Max, Team, or Enterprise — no API key)[/dim]")
            print("  [2] Use an API key                [dim](Anthropic Console sk-ant-* key)[/dim]")
            print("  [3] Enterprise/custom gateway")
            if existing_mode:
                print(f"[dim]Current setup: {existing_mode} — press Enter to keep it.[/dim]")
            choice = typer.prompt("Choose", default=default_choice).strip()
            if choice == "1":
                mode = AUTH_SUBSCRIPTION
            elif choice == "2":
                mode = AUTH_API_KEY
            elif choice == "3":
                mode = AUTH_ENTERPRISE

    # Existing Claude plan auth forwards the client's OAuth bearer upstream, which must stay on this
    # machine — refuse to bind subscription mode to a non-loopback gateway (mirrors `configure
    # claude-code`, and the gateway's own production bearer block). Checked before any prompt/write.
    if mode == AUTH_SUBSCRIPTION and not _is_loopback_gateway(base_url):
        print("[red]✗[/red] Existing Claude plan auth is local-only. Use a local gateway URL (e.g. "
              f"{DEFAULT_BASE_URL}), or choose --auth-mode api_key.")
        raise typer.Exit(2)

    # 2. The key (BYOK) or the enterprise base URL.
    if mode == AUTH_SUBSCRIPTION:
        print("[green]✓[/green] Using your existing Claude plan login.")
        print("[dim]Decompute will not ask for, store, or manage your Claude login token.[/dim]")
        if KEY_FILE.exists():
            print(f"[yellow]⚠[/yellow] Found an existing API key at {KEY_FILE}. Subscription mode ignores it and keeps it for later.")
        if os.getenv("ANTHROPIC_API_KEY") or os.getenv("DECOMPUTE_CLAUDE_KEY"):
            print("[yellow]⚠[/yellow] Your shell currently exports an API key. Claude Code may prefer that over your Claude plan until you unset it.")
    elif mode == AUTH_API_KEY:
        key = claude_key or os.getenv("ANTHROPIC_API_KEY")
        if not key and not yes:
            print("\n[bold]Paste your Anthropic API key[/bold] (it starts with [cyan]sk-ant-[/cyan]…).")
            print("[dim]• It stays HIDDEN as you paste — nothing will appear on screen. That's normal. Paste once, then press Enter.[/dim]")
            print("[dim]• Get one at https://console.anthropic.com/settings/keys[/dim]")
            while True:  # re-prompt until the key is valid (or the user chooses to skip)
                key = typer.prompt("Anthropic API key", hide_input=True, default="").strip()
                if not key:
                    if typer.confirm("No key entered — skip for now (local/mock only)?", default=False):
                        key = ""
                        break
                    continue
                if not key.startswith("sk-ant-"):
                    print("[yellow]⚠ That doesn't look like an Anthropic key (they start with 'sk-ant-'). Paste it again.[/yellow]")
                    continue
                print("[dim]  verifying the key with Anthropic…[/dim]")
                status = _check_anthropic_key(key)
                if status == "ok":
                    print("[green]✓ key verified[/green]")
                    break
                if status == "bad":
                    print("[yellow]⚠ Anthropic rejected that key (invalid or unauthorized). Paste it again.[/yellow]")
                    continue
                print("[dim]  (couldn't reach Anthropic to verify, but the format looks right — saving it.)[/dim]")
                break
        elif key and not key.startswith("sk-ant-"):
            print("[yellow]⚠[/yellow] Anthropic keys usually start with 'sk-ant-'. Saving it anyway.")
        if key:
            save_claude_key(key)
            print(f"[green]✓[/green] Claude key saved (chmod 600) → {CONFIG_DIR / 'claude.key'}")
        else:
            print("[dim]No key yet — fine for local/mock mode. Add one later: [bold]decompute claude --set-key[/bold].[/dim]")
    else:
        upstream_base_url = (typer.prompt("Enterprise Claude gateway URL") if not yes else "").strip().rstrip("/")
        ekey = claude_key or (typer.prompt("Enterprise key (optional, blank to skip)", hide_input=True, default="").strip() if not yes else "")
        if ekey:
            save_claude_key(ekey)

    # 3. Privacy mode.
    pii_mode, secret_mode = "tokenize", "redact"
    if not yes:
        print("\nPrivacy mode:")
        print("  [1] tokenize PII + redact secrets   [dim](recommended)[/dim]")
        print("  [2] redact PII + redact secrets")
        print("  [3] pass PII through + redact secrets")
        choice = typer.prompt("Choose", default="1").strip()
        pii_mode, secret_mode = _PII_MODES.get(int(choice) if choice.isdigit() else 1, _PII_MODES[1])

    # 4. CCR.
    ccr_enabled = True
    if not yes:
        print("\nCCR (Compressed Context Reuse):")
        print("  [1] encrypted local CCR   [dim](recommended)[/dim]")
        print("  [2] disabled")
        ccr_enabled = typer.prompt("Choose", default="1").strip() != "2"

    base_url = base_url.rstrip("/")
    claude_section: dict = {"mode": mode}
    if upstream_base_url:
        claude_section["upstream_base_url"] = upstream_base_url
    save_config({
        "gateway": {"base_url": base_url, "auto_start": True},
        "privacy": {"pii_mode": pii_mode, "secret_mode": secret_mode},
        "ccr": {"enabled": ccr_enabled, "retention_days": 7},
        "claude": claude_section,
    })
    print(f"\n[green]✓[/green] saved {CONFIG_PATH}")
    print(f"  upstream: [cyan]{mode}[/cyan] · privacy: [cyan]{pii_mode}+{secret_mode}[/cyan] · CCR: [cyan]{'on' if ccr_enabled else 'off'}[/cyan]")
    # One-time transparency notice (opt-out telemetry). Counts only — never prompts/files.
    print("[dim]  Decompute shares a weekly, anonymous savings total (tokens saved — never your prompts or "
          "files) to improve the product. Turn it off any time: [bold]decompute telemetry off[/bold].[/dim]")

    # 5. Start gateway after setup?
    start = yes or typer.confirm("\nStart gateway after setup?", default=True)
    if start:
        if _gateway_running(base_url):
            print("[green]✓ gateway already running[/green]")
        elif _start_gateway(base_url):
            print("[green]✓ gateway started[/green]")

    # 6. Configure Claude Code wrapper?
    wire = yes or typer.confirm("Configure Claude Code wrapper?", default=True)
    if wire:
        print("Launch Claude Code through Decompute any time with [bold]decompute claude[/bold].")
        print("For a VS Code project: [bold]decompute vscode .[/bold]")
    else:
        print("Next: [bold]decompute claude[/bold] when you're ready.")
    # Point first-time users straight at the dashboard — this is the #1 thing people couldn't find.
    print(f"\n[bold]Your savings dashboard:[/bold] [bold cyan]{base_url.rstrip('/')}/dashboard[/bold cyan]")
    print("  Open it any time with [bold]decompute dashboard[/bold].")


def _check_anthropic_key(key: str) -> str:
    """Live-validate an Anthropic API key with a cheap, free call (GET /v1/models).

    Returns "ok" (verified), "bad" (Anthropic rejected it → re-prompt), or "unknown" (couldn't reach
    Anthropic — accept the format-valid key and let the gateway surface any issue later)."""
    import httpx
    try:
        r = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=10.0,
        )
        if r.status_code == 200:
            return "ok"
        if r.status_code in (401, 403):
            return "bad"
        return "unknown"
    except Exception:
        return "unknown"


def _verify_engine(base_url: str, *, verified_mode: bool) -> bool:
    """Steps 3-5: minimum doctor checks, YSCompress engine, and fallback-disabled-in-verified-mode.

    Returns True if it is safe to launch. In --verified mode a silent compression fallback
    (rust core not engaged) is a hard stop — the user is paying for compression they wouldn't get.
    Otherwise it warns and lets the launch proceed.
    """
    import httpx
    # 3. minimum doctor checks — the gateway must be ready (db + an upstream credential path).
    try:
        httpx.get(f"{base_url}/readyz", timeout=5.0).raise_for_status()
        print("  [green]✓[/green] gateway ready (db + upstream configured)")
    except Exception as exc:
        print(f"  [red]✗[/red] readiness check failed: {exc}")
        return False
    # 4 + 5. YSCompress engine + fallback guard.
    try:
        caps = (httpx.get(f"{base_url}/health", timeout=5.0).json().get("capabilities") or {})
    except Exception as exc:
        print(f"  [yellow]⚠[/yellow] could not read capabilities: {exc}")
        return not verified_mode
    rust = caps.get("rust_core") or {}
    engine_active = bool(rust.get("active"))
    if engine_active:
        print(f"  [green]✓[/green] YSCompress engine engaged — {rust.get('detail', '')}")
    else:
        msg = rust.get("detail") or "rust core not importable"
        if verified_mode:
            print(f"  [red]✗[/red] verified mode: compression would silently fall back ({msg}). Refusing to launch.")
            print("    Re-run without [bold]--verified[/bold] to launch anyway on the local fallback.")
            return False
        print(f"  [yellow]⚠[/yellow] YSCompress engine not engaged — local fallback in use ({msg}).")
    ml = caps.get("kompress_ml") or {}
    print(f"  [dim]ML text compression: {'on' if ml.get('active') else 'off (rust-core only)'}[/dim]")
    return True


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def claude(
    ctx: typer.Context,
    session_id: Optional[str] = typer.Option(None, "--session-id", help="reuse a specific session id (default: mint a fresh one)"),
    no_start: bool = typer.Option(False, "--no-start", help="don't auto-start the gateway if it's down"),
    verified: bool = typer.Option(False, "--verified", help="hard-fail if compression would silently fall back to the local engine"),
    print_env: bool = typer.Option(False, "--print-env", help="print the export lines instead of launching Claude Code"),
    set_key: bool = typer.Option(False, "--set-key", help="prompt for and store your Claude key, then continue"),
):
    """Launch Claude Code through the Decompute gateway.

    Runs the launch sequence — load config → start gateway → readiness + engine checks →
    set ANTHROPIC_BASE_URL (+ API key only in API-key mode) → exec `claude`. Any extra args are
    forwarded (e.g. `decompute claude --resume`). Each run mints a fresh x-decompute-session-id so
    the session can be scoped in a support bundle.
    """
    # 1. Load config.
    base_url = cfg_base_url()
    cfg = load_config()
    auth_mode = _claude_mode(cfg)
    if set_key:
        key = typer.prompt("Anthropic API key (sk-ant-…)", hide_input=True).strip()
        if key:
            save_claude_key(key)
            print("[green]✓[/green] Claude key saved.")
    claude_key = (load_claude_key() or "") if auth_mode != AUTH_SUBSCRIPTION else ""

    # 2. Start gateway if not running.
    if not _gateway_running(base_url):
        auto = cfg.get("gateway", {}).get("auto_start", True)
        if no_start or not auto:
            print(f"[red]✗[/red] gateway not running at {base_url} and auto-start is off.")
            raise typer.Exit(1)
        print("Gateway not running — starting it…")
        if not _start_gateway(base_url):
            raise typer.Exit(1)
    else:
        print(f"[green]✓[/green] gateway up at {base_url}")

    # 3-5. Readiness + engine + fallback checks.
    if not _verify_engine(base_url, verified_mode=verified):
        raise typer.Exit(1)

    # 6 + 7. Build the child env: route to the gateway and hand it the BYOK key safely.
    sid = session_id or _new_session_id()
    set_session_id(sid)
    if not claude_key and auth_mode == AUTH_API_KEY:
        print("[yellow]⚠[/yellow] no Claude key stored — set one with [bold]decompute claude --set-key[/bold].")
        print("  (continuing; this only works if the gateway is in mock/local mode.)")
    if auth_mode == AUTH_SUBSCRIPTION and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("DECOMPUTE_CLAUDE_KEY")):
        print("[yellow]⚠[/yellow] subscription mode: removing API-key env vars for the Claude Code child process.")
    env_lines = _claude_env_lines(base_url, claude_key, sid, include_key=auth_mode != AUTH_SUBSCRIPTION)
    print(f"[green]✓[/green] routing Claude Code → {base_url}  ·  session [cyan]{sid}[/cyan]")
    print(f"  [bold]Dashboard:[/bold] [cyan]{_dashboard_url()}[/cyan]  [dim]· `decompute dashboard` opens it[/dim]")

    if print_env:
        for k, v in env_lines.items():
            shown = (v[:12] + "…") if k == "ANTHROPIC_API_KEY" else v
            print(f'export {k}="{shown}"')
        return

    # 8. Exec `claude`, replacing this process. The key lives only in the child env — never on disk here.
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("[red]✗[/red] Claude Code (`claude`) isn't on your PATH.")
        print("  Install it from https://claude.com/claude-code, then re-run [bold]decompute claude[/bold].")
        raise typer.Exit(127)
    child_env = {**os.environ, **env_lines}
    if auth_mode == AUTH_SUBSCRIPTION:
        child_env.pop("ANTHROPIC_API_KEY", None)
        child_env.pop("DECOMPUTE_CLAUDE_KEY", None)
    print("[dim]launching `claude`…[/dim]\n")
    if _IS_WINDOWS:
        # os.execve on Windows can't reliably exec a .cmd/.bat launcher (claude ships as claude.cmd) and
        # doesn't truly replace the process. Run it as a child and exit with its code instead — same UX
        # (this CLI blocks until claude exits, then returns its status). shutil.which already resolved
        # the .cmd/.exe via PATHEXT above.
        try:
            rc = subprocess.run([claude_bin, *ctx.args], env=child_env).returncode
        except KeyboardInterrupt:
            rc = 130
        raise typer.Exit(rc)
    os.execve(claude_bin, [claude_bin, *ctx.args], child_env)


@app.command()
def vscode(
    path: Path = typer.Argument(Path("."), help="project directory to configure (default: .)"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="reuse a specific session id (default: mint a fresh one)"),
):
    """Configure a VS Code project so Claude Code in its terminal routes through the gateway.

    Writes only NON-SECRET routing (base URL + session header) into .vscode/settings.json —
    never your Claude key, which could leak if the file is committed. The key is read from
    your environment / ~/.decompute/claude.key at launch (e.g. via `decompute claude`).
    """
    base_url, claude_key = _conn()
    sid = session_id or _new_session_id()
    vsdir = path / ".vscode"
    vsdir.mkdir(parents=True, exist_ok=True)
    settings_path = vsdir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}
    env_block = _claude_env_lines(base_url, claude_key, sid, include_key=False)  # never the key in a project file
    for key in ("terminal.integrated.env.osx", "terminal.integrated.env.linux", "terminal.integrated.env.windows"):
        block = settings.get(key)
        if not isinstance(block, dict):
            block = {}
        block.update(env_block)
        settings[key] = block
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    set_session_id(sid)
    print(f"[green]✓[/green] configured {settings_path}  [dim](routing only — no key written)[/dim]")
    print(f"  Session id: [cyan]{sid}[/cyan]")
    print("In the VS Code integrated terminal, launch with [bold]decompute claude[/bold] so your key is injected safely,")
    print("or export [bold]ANTHROPIC_API_KEY[/bold] in your shell. Then `claude` routes through the gateway.")


def _stop_gateway_windows(port: int) -> bool:
    """Windows: find the PID LISTENING on the gateway port via `netstat -ano`, then `taskkill /F`.
    Windows has no lsof/pkill/SIGTERM; this is the standard equivalent and needs no extra deps."""
    killed = False
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        out = ""
    pids: set[str] = set()
    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        # Columns: Proto  Local-Address  Foreign-Address  State  PID. We want LISTENING sockets whose
        # local address ends with :PORT, and the trailing numeric PID.
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(needle) and parts[-1].isdigit():
            pids.add(parts[-1])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F", "/T"], capture_output=True, timeout=5)
            killed = True
        except Exception:
            pass
    return killed


def _stop_gateway(base_url: str) -> bool:
    """Best-effort stop: terminate whatever is listening on the gateway port, then the uvicorn app."""
    from urllib.parse import urlparse

    port = urlparse(base_url).port or 8080
    if _IS_WINDOWS:
        return _stop_gateway_windows(port)

    import signal
    killed = False
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
        for pid in (p for p in out.split() if p.strip().isdigit()):
            try:
                os.kill(int(pid), signal.SIGTERM)
                killed = True
            except Exception:
                pass
    except Exception:
        pass
    if not killed:  # fallback: match the uvicorn target
        try:
            subprocess.run(["pkill", "-f", "decompute_gateway.main:app"], timeout=5)
            killed = True
        except Exception:
            pass
    return killed


def _ask(prompt: str, *, default: bool, noninteractive: bool) -> bool:
    """Confirm a step. When non-interactive (--yes/--purge), resolve to `default` without prompting."""
    return default if noninteractive else typer.confirm(prompt, default=default)


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="non-interactive: accept the SAFE defaults (stop gateway + remove binary; keep config/data/key)."),
    purge: bool = typer.Option(False, "--purge", help="non-interactive: also remove config, local usage/CCR data, and the stored Claude key."),
):
    """Uninstall Decompute. Confirms each step; never removes your config, data, or Claude key by default."""
    base_url = cfg_base_url()
    noninteractive = yes or purge
    removed: list[str] = []

    # 1. Stop gateway service?  [Y/n]
    if _ask("Stop the Decompute gateway service?", default=True, noninteractive=noninteractive):
        if _stop_gateway(base_url):
            print("[green]✓[/green] gateway stopped")
        else:
            print("[dim]No running gateway found (or it wasn't started by us).[/dim]")

    # 2. Remove CLI binary/symlink?  [Y/n]
    if _ask("Remove the CLI binary/symlink?", default=True, noninteractive=noninteractive):
        for shim in (Path.home() / ".local" / "bin" / "decompute", *( [Path(p)] if (p := shutil.which("decompute")) else [] )):
            try:
                if shim.is_symlink() or shim.exists():
                    shim.unlink(missing_ok=True)
                    removed.append(str(shim))
            except Exception:
                pass
        print("[dim]If you installed via pip/pipx, also run: pip uninstall decompute-ai[/dim]")

    # 3. Remove config?  [y/N]  (default NO — purge forces yes)
    if purge or _ask("Remove config (config.toml)?", default=False, noninteractive=noninteractive):
        for f in (CONFIG_TOML, CONFIG_DIR / "claude.env", LEGACY_JSON):
            if f.exists():
                f.unlink(missing_ok=True)
                removed.append(str(f))

    # 4. Remove local usage/CCR data?  [y/N]  (default NO — purge forces yes)
    if purge or _ask("Remove local usage/CCR data?", default=False, noninteractive=noninteractive):
        for p in (CONFIG_DIR / "data", CONFIG_DIR / "ccr"):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(str(p))
        for f in [*CONFIG_DIR.glob("*.db"), *CONFIG_DIR.glob("*.sqlite*"), CONFIG_DIR / "gateway.log"]:
            if f.exists():
                f.unlink(missing_ok=True)
                removed.append(str(f))

    # 5. Remove stored Claude key + device license?  [y/N]  (default NO — purge forces yes)
    if purge or _ask("Remove the stored Claude key + device license?", default=False, noninteractive=noninteractive):
        if KEY_FILE.exists():
            clear_claude_key()
            removed.append(str(KEY_FILE))
        if LICENSE_FILE.exists():
            clear_license_key()
            removed.append(str(LICENSE_FILE))
        if DEVICE_ID_FILE.exists():
            DEVICE_ID_FILE.unlink(missing_ok=True)
            removed.append(str(DEVICE_ID_FILE))

    # Tidy: drop ~/.decompute only if we emptied it.
    try:
        if CONFIG_DIR.exists() and not any(CONFIG_DIR.iterdir()):
            CONFIG_DIR.rmdir()
            removed.append(str(CONFIG_DIR))
    except Exception:
        pass

    if removed:
        print("[green]✓ removed[/green]\n  " + "\n  ".join(removed))
    else:
        print("Nothing removed — your config, data, and Claude key are intact.")
    print("\nIf you ran [bold]decompute vscode[/bold], remove the env block from .vscode/settings.json, "
          "and any [bold]source ~/.decompute/claude.env[/bold] line from your shell rc, manually.")


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON (used by `support bundle`)."),
    open_dashboard: bool = typer.Option(False, "--open", help="Open the savings dashboard in your browser when the gateway is healthy."),
):
    """Diagnose the gateway connection, auth, and capabilities."""
    client = _client()
    cfg = load_config()
    auth_mode = _claude_mode(cfg)
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("base_url", True, client.base_url)
    if auth_mode == AUTH_SUBSCRIPTION:
        detail = "existing Claude plan login; no API key stored by Decompute"
        if load_claude_key():
            detail += " (local API key file exists but subscription mode ignores it)"
        add("Claude auth", True, detail)
    else:
        add("Claude auth", True, (client.api_key[:10] + "…") if client.api_key else "API key not set — local/mock only")

    try:
        h = client.get("/health")
        add("gateway /health", bool(h.get("ok")), f"env={h.get('environment')} mock={h.get('mock_anthropic')}")
        caps = h.get("capabilities") or {}
        active = [k for k, v in caps.items() if isinstance(v, dict) and v.get("active")]
        add("capabilities", True, ", ".join(active) or "none active")
    except DecomputeError as exc:
        add("gateway /health", False, str(exc))

    try:
        client.get("/readyz")
        add("gateway /readyz", True, "db + upstream reachable")
    except DecomputeError as exc:
        add("gateway /readyz", False, str(exc))

    try:
        u = client.usage.summary()
        add("auth (/v1/usage)", True, f"{u.get('requests', 0)} requests recorded")
    except DecomputeError as exc:
        add("auth (/v1/usage)", False, str(exc))

    ok_all = all(c["ok"] for c in checks)
    gateway_ok = any(c["check"] == "gateway /health" and c["ok"] for c in checks)
    if json_output:
        print(json.dumps({"ok": ok_all, "checks": checks, "dashboard_url": _dashboard_url()}, indent=2))
    else:
        table = Table(title="decompute doctor", show_header=True, header_style="bold")
        table.add_column("check"); table.add_column("status"); table.add_column("detail")
        for c in checks:
            table.add_row(str(c["check"]), "[green]ok[/green]" if c["ok"] else "[red]FAIL[/red]", str(c["detail"]))
        print(table)
        # Always surface the dashboard URL — first-time users often can't find it otherwise.
        if gateway_ok:
            print(f"\n[bold]Dashboard:[/bold] [bold cyan]{_dashboard_url()}[/bold cyan]  "
                  "[dim](open it with `decompute dashboard`)[/dim]")
            if open_dashboard:
                print("[dim]opening dashboard in your browser…[/dim]")
                _open(_dashboard_url())
    raise typer.Exit(code=0 if ok_all else 1)


@app.command()
def status():
    """One-glance health: config, routing, gateway, and your savings so far."""
    base_url = cfg_base_url()
    cfg = load_config()
    auth_mode = _claude_mode(cfg)
    stored_key = load_claude_key() or ""
    claude_key = stored_key if auth_mode != AUTH_SUBSCRIPTION else ""
    routed = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") == base_url.rstrip("/")
    t = Table(title="decompute status", show_header=False)
    t.add_column("k", style="bold"); t.add_column("v")
    t.add_row("gateway", base_url)
    t.add_row("Claude auth", "existing Claude plan" if auth_mode == AUTH_SUBSCRIPTION else auth_mode)
    if auth_mode == AUTH_SUBSCRIPTION:
        t.add_row("API key", "[dim]ignored in subscription mode[/dim]" if stored_key else "[dim]not needed[/dim]")
    else:
        t.add_row("API key", (claude_key[:10] + "…") if claude_key else "[yellow]not set — `decompute claude --set-key`[/yellow]")
    t.add_row("session id", get_session_id() or "[dim]none — run `decompute claude`[/dim]")
    t.add_row("Claude Code routed (this shell)",
              "[green]yes[/green]" if routed else "[yellow]no — run `decompute claude`[/yellow]")
    client = _client()
    try:
        h = client.get("/health")
        caps = h.get("capabilities") or {}
        active = ", ".join(k for k, v in caps.items() if isinstance(v, dict) and v.get("active")) or "none"
        t.add_row("gateway", f"[green]ok[/green]  env={h.get('environment')} mock={h.get('mock_anthropic')}")
        t.add_row("capabilities", active)
    except DecomputeError as exc:
        t.add_row("gateway", f"[red]unreachable[/red] — {exc}")
    try:
        lic = client.licenses.status()
        if lic.get("license_status") == "active":
            t.add_row("license", f"[green]Pro[/green] (device) · renews {str(lic.get('current_period_end') or '')[:10] or '—'}")
        else:
            rem, lim = lic.get("quota_remaining"), lic.get("free_quota_limit")
            if rem is not None:
                t.add_row("license", f"Free · compression quota {rem}/{lim} left today · [dim]decompute upgrade[/dim]")
    except DecomputeError:
        pass
    try:
        u = client.usage.summary()
        t.add_row("usage", f"{u.get('requests', 0)} requests · {u.get('tokens_saved', 0):,} tokens saved · "
                           f"${u.get('estimated_savings_usd', 0.0):.4f} saved")
    except DecomputeError:
        pass
    t.add_row("dashboard", f"{_dashboard_url()}  [dim](open with `decompute dashboard`)[/dim]")
    print(t)


@app.command()
def version(check: bool = typer.Option(False, "--check", help="check PyPI for a newer release")):
    """Show the installed CLI version (and optionally check for updates)."""
    try:
        from importlib.metadata import version as _pkgver
        cur = _pkgver("decompute-ai")
    except Exception:
        cur = "0.0.0+dev"
    print(f"decompute-ai {cur}")
    if check:
        try:
            import httpx
            resp = httpx.get("https://pypi.org/pypi/decompute-ai/json", timeout=5.0)
            if resp.status_code != 200:
                print("[dim]not yet on PyPI — nothing to compare against.[/dim]")
                return
            latest = resp.json()["info"]["version"]
            if latest != cur:
                print(f"[yellow]update available:[/yellow] {latest} — run [bold]pip install -U decompute-ai[/bold]")
            else:
                print("[green]✓ up to date[/green]")
        except Exception as exc:
            print(f"[dim]couldn't reach PyPI ({exc}).[/dim]")


@app.command()
def telemetry(action: Optional[str] = typer.Argument(None, help="on | off | status (default: status)")):
    """Anonymous savings telemetry — controls the WEEKLY counts-only savings rollup.

    Decompute sends a weekly total of how many tokens you've saved (counts only — NEVER your prompts,
    files, or any content) so we can see aggregate savings. It's on by default; turn it off any time.
    Honors DO_NOT_TRACK and DECOMPUTE_TELEMETRY=off too.
    """
    act = (action or "status").strip().lower()
    if act in ("on", "enable", "true"):
        save_config({"telemetry": {"enabled": True}})
        print("[green]✓[/green] Savings telemetry ON — a weekly, counts-only savings total is shared (never your prompts/files).")
        return
    if act in ("off", "disable", "false"):
        save_config({"telemetry": {"enabled": False}})
        print("[green]✓[/green] Savings telemetry OFF — nothing is sent. (Re-enable with `decompute telemetry on`.)")
        return
    if act != "status":
        print("[yellow]Usage:[/yellow] decompute telemetry [on|off|status]")
        raise typer.Exit(2)
    # status
    cfg = load_config()
    enabled = (cfg.get("telemetry") or {}).get("enabled", True)
    dnt = bool(os.getenv("DO_NOT_TRACK")) or (os.getenv("DECOMPUTE_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no"))
    on = bool(enabled) and not dnt
    t = Table(title="savings telemetry", show_header=False)
    t.add_column("k", style="bold"); t.add_column("v")
    t.add_row("status", "[green]on[/green] (weekly, counts only)" if on else "[dim]off[/dim]")
    if dnt and enabled:
        t.add_row("note", "disabled by DO_NOT_TRACK / DECOMPUTE_TELEMETRY in your environment")
    t.add_row("what's sent", "request count, tokens saved, est. $ saved, OS, version")
    t.add_row("never sent", "prompts, files, messages, or any content")
    t.add_row("toggle", "decompute telemetry on  ·  decompute telemetry off")
    print(t)


@app.command()
def limits():
    """Show your quota (rate limits + monthly spend cap) and current consumption."""
    try:
        d = _client().get("/v1/limits")
    except DecomputeError as exc:
        print(f"[red]could not fetch limits:[/red] {exc}")
        raise typer.Exit(1)
    lim = d.get("limits", {})
    # Self-hosted / BYOK gateways report metered=false: no Decompute quota applies —
    # your own Anthropic account limits are the ceiling. Show that plainly.
    if d.get("metered") is False:
        t = Table(title=f"limits — {d.get('project_id', '')}")
        t.add_column("quota"); t.add_column("value")
        t.add_row("metering", "[green]unmetered[/green] (self-hosted / BYOK)")
        t.add_row("ceiling", "your own Anthropic account limits")
        t.add_row("monthly usage (est.)", f"${d.get('monthly_spend_usd', 0.0):.4f}")
        print(t)
        return
    t = Table(title=f"limits — {d.get('project_id', '')}")
    t.add_column("quota"); t.add_column("value")
    t.add_row("requests / min (per key)", str(lim.get("per_key_rpm")))
    t.add_row("tokens / min (project)", str(lim.get("project_tokens_per_min")))
    t.add_row("max context tokens", str(lim.get("max_context_tokens")))
    cap = d.get("monthly_spend_cap_usd")
    t.add_row("monthly spend", f"${d.get('monthly_spend_usd', 0.0):.4f}" + (f" / ${cap} cap" if cap is not None else ""))
    if d.get("tier"):
        t.add_row("plan tier", d["tier"])
    print(t)


def _open(url: str) -> None:
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _dashboard_url() -> str:
    """The savings dashboard URL for the configured gateway (e.g. http://127.0.0.1:8080/dashboard)."""
    return cfg_base_url().rstrip("/") + "/dashboard"


@app.command()
def dashboard(
    no_open: bool = typer.Option(False, "--no-open", help="Print the dashboard URL but don't open a browser."),
):
    """Open the Decompute savings dashboard in your browser.

    Starts the gateway first if it isn't running (when auto-start is enabled).
    """
    base_url = cfg_base_url()
    url = _dashboard_url()

    # Make sure the gateway is up so the page actually loads, mirroring `decompute claude`.
    if not _gateway_running(base_url):
        cfg = load_config()
        if not (cfg.get("gateway", {}).get("auto_start", True)):
            print(f"[yellow]⚠[/yellow] gateway isn't running at [cyan]{base_url}[/cyan] and auto-start is off.")
            print(f"  Start it, then open the dashboard:  [bold]{url}[/bold]")
            raise typer.Exit(1)
        print("Gateway not running — starting it…")
        if not _start_gateway(base_url):
            print(f"[red]✗[/red] couldn't start the gateway. Once it's up, open:  [bold]{url}[/bold]")
            raise typer.Exit(1)

    print(f"[green]✓[/green] Dashboard:  [bold cyan]{url}[/bold cyan]")
    if no_open:
        return
    print("[dim]opening in your browser…[/dim]")
    _open(url)


@app.command()
def upgrade():
    """Subscribe to Pro for THIS device — opens Stripe Checkout, then activate."""
    client = _client()
    try:
        status = client.get("/v1/billing/status")
    except DecomputeError as exc:
        print(f"[red]could not reach the gateway:[/red] {exc}")
        raise typer.Exit(1)
    if not status.get("billing_enabled"):
        print("[yellow]Billing isn't configured on this gateway (local/dev).[/yellow]")
        raise typer.Exit(1)
    dl = status.get("device_license") or {}
    if dl.get("license_status") == "active":
        print("This device is already on [bold]Pro[/bold]. Manage it with [bold]decompute billing --portal[/bold].")
        raise typer.Exit(0)
    try:
        r = client.post("/v1/billing/checkout")
    except DecomputeError as exc:
        print(f"[red]checkout failed:[/red] {exc}")
        raise typer.Exit(1)
    url, session_id = r.get("checkout_url"), r.get("session_id")
    print("Complete your subscription in the browser:")
    print(f"  [bold]{url}[/bold]\n")
    print("After paying, activate Pro on this device:")
    print(f"  [bold]decompute activate --session {session_id}[/bold]")
    print("[dim](If the gateway is reachable by Stripe, the webhook activates it automatically.)[/dim]")
    _open(url)


@app.command()
def activate(
    license_key: Optional[str] = typer.Argument(None, help="a Pro license key (DCMP-…) to bind to THIS device"),
    session: Optional[str] = typer.Option(None, "--session", help="redeem a PAID Stripe checkout session id (cs_…) and activate"),
):
    """Activate Pro on this device — redeem a paid checkout (--session) or bind a license key."""
    client = _client()
    if session:
        try:
            r = client.licenses.redeem(session)
        except DecomputeError as exc:
            print(f"[red]could not redeem checkout:[/red] {exc}")
            raise typer.Exit(1)
        key = r.get("license_key")
        if key:
            save_license_key(key)
            print(f"[green]✓ Pro activated on this device[/green] — license key saved to {LICENSE_FILE}.")
        else:
            print("[green]✓ this device is activated[/green] (license already issued for that checkout).")
        save_config({"license": {"tier": r.get("tier", "pro"), "device_activated": True, "last_checkout_session": session}})
        return
    if license_key:
        try:
            r = client.licenses.activate(license_key)
        except DecomputeError as exc:
            print(f"[red]could not activate:[/red] {exc}")
            raise typer.Exit(1)
        save_license_key(license_key)
        save_config({"license": {"tier": r.get("tier", "pro"), "device_activated": True}})
        renews = str(r.get("current_period_end") or "")[:10]
        print(f"[green]✓ Pro activated on this device[/green]" + (f" — renews {renews}." if renews else "."))
        return
    print("Provide a license key, or [bold]--session <cs_…>[/bold] from your checkout. Start with [bold]decompute upgrade[/bold].")
    raise typer.Exit(1)


@app.command("license")
def license_status():
    """Show this device's Pro license / free compression quota."""
    client = _client()
    try:
        s = client.licenses.status()
    except DecomputeError as exc:
        print(f"[red]could not fetch license status:[/red] {exc}")
        raise typer.Exit(1)
    t = Table(title="device license")
    t.add_column("field"); t.add_column("value")
    t.add_row("device id", load_device_id())
    if s.get("license_status") == "active":
        t.add_row("tier", "[green]Pro[/green]")
        t.add_row("renews / ends", str(s.get("current_period_end") or "—")[:10])
    else:
        t.add_row("tier", "Free")
        rem, lim = s.get("quota_remaining"), s.get("free_quota_limit")
        if rem is not None:
            t.add_row("compression quota", f"{rem}/{lim} left this window")
        t.add_row("upgrade", "decompute upgrade")
    print(t)


@app.command()
def billing(portal: bool = typer.Option(False, "--portal", help="open the Stripe billing portal to manage/cancel")):
    """Show your plan + subscription status (or --portal to manage it)."""
    client = _client()
    if portal:
        try:
            r = client.post("/v1/billing/portal")
        except DecomputeError as exc:
            print(f"[red]could not open the billing portal:[/red] {exc}")
            raise typer.Exit(1)
        url = r.get("portal_url")
        print(f"Manage your subscription:\n  [bold]{url}[/bold]")
        _open(url)
        return
    try:
        s = client.get("/v1/billing/status")
    except DecomputeError as exc:
        print(f"[red]could not fetch billing status:[/red] {exc}")
        raise typer.Exit(1)
    t = Table(title="billing")
    t.add_column("field"); t.add_column("value")
    t.add_row("plan tier", s.get("tier", "free"))
    t.add_row("subscription", s.get("subscription_status") or "—")
    t.add_row("renews / ends", s.get("current_period_end") or "—")
    t.add_row("billing enabled", "yes" if s.get("billing_enabled") else "no (local/dev)")
    print(t)
    if s.get("tier", "free") == "free" and s.get("billing_enabled"):
        print("Upgrade with [bold]decompute upgrade[/bold].")


# NOTE: Decompute issues no API keys (BYOK). There is no `keys` sub-app or `api-key-create`
# command — you bring your own Claude key, stored locally via `decompute init` / `decompute claude`.

support_app = typer.Typer(help="Support / debugging utilities.")
app.add_typer(support_app, name="support")


def _auth_headers(extra: dict | None = None) -> dict:
    """Auth header for gateway calls: BYOK x-api-key when set, else none (local_noauth)."""
    h = dict(extra or {})
    claude_key = "" if _claude_mode() == AUTH_SUBSCRIPTION else (load_claude_key() or "")
    if claude_key:
        h["x-api-key"] = claude_key
    return h


def _upload_support_bundle(zip_path: Path) -> dict:
    """POST the bundle zip to the gateway's support inbox; returns the server's response."""
    import httpx
    base_url, _ = _conn()
    resp = httpx.post(
        f"{base_url}/v1/support/upload",
        content=zip_path.read_bytes(),
        headers=_auth_headers({"content-type": "application/zip"}),
        timeout=60.0,
    )
    if resp.status_code != 200:
        raise DecomputeError(f"upload failed: HTTP {resp.status_code} — {resp.text[:300]}")
    return resp.json()


def _make_support_bundle(*, session, all_sessions, since, until, output_dir, data_dir,
                         project_root, pii_mode, release_version, canary, dry_run, send):
    """Request a privacy-safe support bundle from the gateway, download it, and optionally send it.

    The gateway builds the bundle server-side (POST /v1/support/bundle), scrubs it, runs the
    leak scan, and streams the zip back. The CLI never imports gateway internals — this works
    from a plain `pip install decompute-ai`.
    """
    import httpx

    # Resolve session scope.
    if not all_sessions and not session:
        session = get_session_id()
        if session:
            print(f"[dim]Scoping to your last session {session} — use --session to override, --all for everything.[/dim]")

    base_url, _ = _conn()
    headers = _auth_headers()

    if dry_run:
        print("[bold]Dry run[/bold] — previewing what the bundle would contain (nothing saved or sent)…")
        payload: dict = {"download": False, "pii_mode": pii_mode}
        try:
            resp = httpx.post(f"{base_url}/v1/support/bundle", json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                raise DecomputeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            d = resp.json()
        except Exception as exc:
            print(f"[red]Could not reach the gateway:[/red] {exc}")
            raise typer.Exit(1)
        manifest = d.get("manifest", {})
        for f in manifest.get("included_files", []):
            print(f"  {f.get('path')}  [dim]({f.get('bytes', '?')} bytes)[/dim]")
        pr = d.get("privacy_report", {})
        ok = d.get("leak_scan", {}).get("ok", False)
        print(f"\nprompt fields omitted: {pr.get('prompt_fields_omitted', 0)} · "
              f"secrets redacted: {pr.get('secret_fields_redacted', 0)} · "
              f"leak scan: {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}")
        print("[dim](nothing written or sent — re-run without --dry-run to create the bundle.)[/dim]")
        raise typer.Exit(0)

    # Build + download the real zip.
    print("Building support bundle on the gateway…")
    payload = {"download": True, "pii_mode": pii_mode}
    try:
        resp = httpx.post(f"{base_url}/v1/support/bundle", json=payload, headers=headers, timeout=120.0)
        if resp.status_code != 200:
            raise DecomputeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        print(f"[red]Could not build the bundle:[/red] {exc}")
        raise typer.Exit(1)

    # Save the downloaded zip locally.
    out_dir = Path(output_dir).expanduser() if output_dir else Path(data_dir).expanduser() / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_name = resp.headers.get("x-decompute-support-bundle", "decompute-support.zip")
    zip_path = out_dir / bundle_name
    zip_path.write_bytes(resp.content)

    print(f"\n[green]✓[/green] Support bundle: {zip_path}")
    if session:
        print(f"  Session scope: {session}" + (f"  since {since}" if since else ""))
    print("  Raw prompts: no · CCR payloads: no · Secrets scrubbed: yes")
    print(f"  Leak scan: {'[green]PASS[/green]' if resp.headers.get('x-decompute-support-leak-scan-ok') == 'true' else '[yellow]check gateway logs[/yellow]'}")

    if send:
        print("\nSending to Decompute support…")
        try:
            r = _upload_support_bundle(zip_path)
            print(f"[green]✓ Sent![/green] Reference id: [bold]{r.get('reference_id')}[/bold]")
            print("  Give us that id and we can pull your scrubbed bundle.")
        except DecomputeError as exc:
            print(f"[red]Send failed:[/red] {exc}")
            print(f"  Your bundle is at {zip_path} — attach it to a support ticket.")
            raise typer.Exit(3)


@support_app.command("bundle")
def support_bundle(
    session: Optional[str] = typer.Option(None, "--session", help="Scope to one Claude Code session id (default: your last `decompute claude` session)."),
    all_sessions: bool = typer.Option(False, "--all", help="Full bundle — do not scope to a session."),
    since: Optional[str] = typer.Option(None, "--since", help="Session activity since this time (ISO 8601, unix epoch, or '2h'/'7d')."),
    until: Optional[str] = typer.Option(None, "--until", help="Session activity up to this time."),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", help="Where to write the zip (default: <data-dir>/support)."),
    data_dir: str = typer.Option(".decompute", "--data-dir", help="Gateway data dir holding logs/config."),
    project_root: str = typer.Option(".", "--project-root"),
    pii_mode: str = typer.Option("tokenize", "--pii-mode", help="tokenize | redact"),
    release_version: str = typer.Option("v0.1.0-beta.1", "--release-version"),
    canary: list[str] = typer.Option([], "--canary", help="A fake secret/prompt that must NOT appear in the bundle (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview exactly what a bundle would contain, then discard it (nothing kept/sent)."),
    send: bool = typer.Option(False, "--send", help="Also upload the bundle to Decompute support and print a reference id."),
):
    """Create a privacy-scrubbed support bundle — no raw prompts, CCR payloads, secrets, or PII.

    Folds in a metadata-only report for your Claude Code session (request IDs, models,
    statuses, token/latency summaries — never prompt content). Add --send to transmit it to us.
    """
    _make_support_bundle(session=session, all_sessions=all_sessions, since=since, until=until,
                         output_dir=output_dir, data_dir=data_dir, project_root=project_root,
                         pii_mode=pii_mode, release_version=release_version, canary=canary,
                         dry_run=dry_run, send=send)


@support_app.command("send")
def support_send(
    session: Optional[str] = typer.Option(None, "--session", help="Scope to one Claude Code session id (default: your last session)."),
    all_sessions: bool = typer.Option(False, "--all", help="Send full diagnostics, not scoped to a session."),
    since: Optional[str] = typer.Option(None, "--since", help="Session activity since this time (ISO 8601 / unix / '2h')."),
    until: Optional[str] = typer.Option(None, "--until", help="Session activity up to this time."),
    data_dir: str = typer.Option(".decompute", "--data-dir", help="Gateway data dir holding logs/config."),
    project_root: str = typer.Option(".", "--project-root"),
    pii_mode: str = typer.Option("tokenize", "--pii-mode", help="tokenize | redact"),
):
    """Create AND send a privacy-safe support bundle to Decompute — returns a reference id.

    Sends only diagnostics (logs, request IDs, capabilities, session summary); raw prompts,
    CCR payloads, secrets, and personal info are stripped + leak-scanned before sending.
    Preview first with `decompute support bundle --dry-run`.
    """
    _make_support_bundle(session=session, all_sessions=all_sessions, since=since, until=until,
                         output_dir=None, data_dir=data_dir, project_root=project_root,
                         pii_mode=pii_mode, release_version="v0.1.0-beta.1", canary=[],
                         dry_run=False, send=True)


@app.command("devices-scan")
def devices_scan():
    print_json(_client().devices.scan())


@app.command("devices-list")
def devices_list():
    print_json(_client().devices.list())


@app.command("compress")
def compress(path: Optional[Path] = None, text: Optional[str] = None, mode: str = "safe"):
    content = text or (path.read_text() if path else typer.prompt("Text"))
    print_json(_client().compression.preview([{"role": "tool", "content": content}], mode=mode))


@app.command("plan")
def plan(model: str = "claude-sonnet-4-5", path: Optional[Path] = None, message: str = "Hello"):
    content = path.read_text() if path else message
    print_json(_client().routes.plan(model=model, messages=[{"role": "user", "content": content}]))


@app.command("chat")
def chat(message: str, model: str = "claude-sonnet-4-5"):
    print_json(_client().chat.completions.create(model=model, messages=[{"role": "user", "content": message}]))


@app.command("usage")
def usage(
    from_: Optional[str] = typer.Option(None, "--from", help="ISO start, e.g. 2026-05-01"),
    to: Optional[str] = typer.Option(None, "--to", help="ISO end, e.g. 2026-06-01"),
    format: str = typer.Option("table", "--format", help="table | json | csv"),
):
    """Show or export usage. With --from/--to/--format csv this exports a date-windowed report."""
    client = _client()
    if format == "table" and not (from_ or to):
        u = client.usage.summary()
        t = Table(title="usage summary")
        for k in ("requests", "raw_input_tokens", "yellowstone_final_input_tokens", "tokens_saved", "estimated_cost_usd", "estimated_savings_usd"):
            t.add_column(k)
        t.add_row(*[str(u.get(k, "")) for k in ("requests", "raw_input_tokens", "yellowstone_final_input_tokens", "tokens_saved", "estimated_cost_usd", "estimated_savings_usd")])
        print(t)
        return
    out = client.usage.export(format="csv" if format == "csv" else "json", start=from_, end=to)
    print(out if format == "csv" else json.dumps(out, indent=2, ensure_ascii=False))


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()
