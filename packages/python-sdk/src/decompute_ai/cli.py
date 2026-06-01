from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich import print
from rich.table import Table

from .client import Decompute
from .config import CONFIG_DIR, CONFIG_PATH, load_config, save_config
from .errors import DecomputeError

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_API_KEY = "dc_dev_local"

app = typer.Typer(help="Decompute Gateway CLI")


def _client() -> Decompute:
    return Decompute()


@app.command()
def health():
    print(_client().get("/health"))


@app.command()
def login(
    base_url: str = typer.Option(..., "--base-url", help="Gateway URL, e.g. http://127.0.0.1:8080"),
    api_key: str = typer.Option(..., "--api-key", help="Decompute API key (dc_...)", prompt=True, hide_input=True),
):
    """Save gateway URL + API key to ~/.decompute/config.json and verify the connection."""
    save_config({"base_url": base_url.rstrip("/"), "api_key": api_key})
    client = Decompute(api_key=api_key, base_url=base_url)
    try:
        client.get("/health")
    except DecomputeError as exc:
        print(f"[yellow]Saved config, but health check failed:[/yellow] {exc}")
        raise typer.Exit(code=1)
    print(f"[green]✓ logged in[/green] — saved to {CONFIG_PATH}")


@app.command("config-set")
def config_set(key: str, value: str):
    """Set a single config value, e.g. `decompute config-set base_url http://host:8080`."""
    path = save_config({key: value})
    print(f"[green]✓[/green] {key} = {value}  ({path})")


@app.command("config-show")
def config_show():
    print(json.dumps(load_config(), indent=2))


def _conn() -> tuple[str, str]:
    """(base_url, api_key) from saved config, falling back to local-dev defaults."""
    cfg = load_config()
    return (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"), (cfg.get("api_key") or DEFAULT_API_KEY)


@app.command()
def init(
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url", help="Gateway URL"),
    api_key: str = typer.Option(DEFAULT_API_KEY, "--api-key", help="Decompute API key (dc_...)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="non-interactive (accept defaults)"),
):
    """Initialize local config (~/.decompute/config.json) and verify the gateway."""
    if not yes:
        base_url = typer.prompt("Gateway URL", default=base_url)
        api_key = typer.prompt("Decompute API key", default=api_key)
    base_url = base_url.rstrip("/")
    save_config({"base_url": base_url, "api_key": api_key})
    print(f"[green]✓[/green] saved {CONFIG_PATH}")
    try:
        h = Decompute(api_key=api_key, base_url=base_url).get("/health")
        print(f"[green]✓ gateway reachable[/green] — env={h.get('environment')} mock={h.get('mock_anthropic')}")
        print("Next: [bold]decompute claude[/bold] (or [bold]decompute vscode .[/bold]) to route your tools through it.")
    except DecomputeError as exc:
        print(f"[yellow]Config saved, but the gateway isn't reachable yet:[/yellow] {exc}")
        print("Start it (scripts/run_gateway.sh), then run [bold]decompute doctor[/bold].")


@app.command()
def claude(
    write_profile: bool = typer.Option(False, "--write-profile", help="append the source line to your shell rc"),
):
    """Configure Claude Code to route through the Decompute gateway (writes a sourceable env file)."""
    base_url, api_key = _conn()
    env_lines = [f'export ANTHROPIC_BASE_URL="{base_url}"', f'export ANTHROPIC_API_KEY="{api_key}"']
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = CONFIG_DIR / "claude.env"
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except Exception:
        pass
    print(f"[green]✓[/green] wrote {env_path}")
    print("Activate it for Claude Code:")
    print(f"  [bold]source {env_path}[/bold]   # then run `claude` as usual")
    if write_profile:
        rc = Path.home() / (".zshrc" if str(os.getenv("SHELL", "")).endswith("zsh") else ".bashrc")
        line = f"source {env_path}"
        existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if line in existing:
            print(f"[green]✓[/green] already in {rc}")
        else:
            rc.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + line + "\n", encoding="utf-8")
            print(f"[green]✓[/green] appended to {rc} — restart your shell")
    else:
        print(f"  make it permanent: [bold]echo 'source {env_path}' >> ~/.zshrc[/bold]")


@app.command()
def vscode(
    path: Path = typer.Argument(Path("."), help="project directory to configure (default: .)"),
):
    """Configure a VS Code project so Claude Code in its terminal routes through the gateway."""
    base_url, api_key = _conn()
    vsdir = path / ".vscode"
    vsdir.mkdir(parents=True, exist_ok=True)
    settings_path = vsdir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}
    env_block = {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": api_key}
    for key in ("terminal.integrated.env.osx", "terminal.integrated.env.linux", "terminal.integrated.env.windows"):
        block = settings.get(key)
        if not isinstance(block, dict):
            block = {}
        block.update(env_block)
        settings[key] = block
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"[green]✓[/green] configured {settings_path}")
    print("Reopen the VS Code integrated terminal; `claude` there now routes through the gateway.")


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
):
    """Remove local Decompute config + credentials (~/.decompute) and the CLI shim."""
    removed = []
    if CONFIG_DIR.exists():
        if not yes and not typer.confirm(f"Remove {CONFIG_DIR} (config, credentials, env files, venv)?"):
            raise typer.Exit(code=1)
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        removed.append(str(CONFIG_DIR))
    shim = Path.home() / ".local" / "bin" / "decompute"
    if shim.exists() or shim.is_symlink():
        shim.unlink(missing_ok=True)
        removed.append(str(shim))
    if removed:
        print("[green]✓ removed[/green] " + ", ".join(removed))
    else:
        print("Nothing to remove — Decompute is not installed in the usual locations.")
    print("If you ran `decompute claude --write-profile` or `decompute vscode`, remove the "
          "`source ~/.decompute/claude.env` line from your shell rc and the env block from "
          ".vscode/settings.json manually.")


@app.command()
def doctor():
    """Diagnose the gateway connection, auth, and capabilities."""
    client = _client()
    table = Table(title="decompute doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    ok_all = True

    def add(name: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        table.add_row(name, "[green]ok[/green]" if ok else "[red]FAIL[/red]", detail)

    add("base_url", True, client.base_url)
    add("api_key", bool(client.api_key), (client.api_key[:10] + "…") if client.api_key else "missing")

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

    print(table)
    raise typer.Exit(code=0 if ok_all else 1)


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


@app.command("api-key-create")
def api_key_create(name: str = "default"):
    print_json(_client().api_keys.create(name=name))


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()
