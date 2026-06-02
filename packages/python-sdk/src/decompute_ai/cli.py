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


def _write_claude_env(base_url: str, api_key: str) -> Path:
    """Write the sourceable Claude Code env file (ANTHROPIC_BASE_URL/API_KEY). Returns its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = CONFIG_DIR / "claude.env"
    env_path.write_text(
        f'export ANTHROPIC_BASE_URL="{base_url}"\nexport ANTHROPIC_API_KEY="{api_key}"\n',
        encoding="utf-8",
    )
    try:
        env_path.chmod(0o600)
    except Exception:
        pass
    return env_path


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
    if not api_key.startswith("dc_"):
        print("[yellow]⚠[/yellow] API keys normally start with 'dc_' — double-check the value if auth fails.")
    save_config({"base_url": base_url, "api_key": api_key})
    print(f"[green]✓[/green] saved {CONFIG_PATH}")
    reachable = False
    try:
        h = Decompute(api_key=api_key, base_url=base_url).get("/health")
        reachable = True
        print(f"[green]✓ gateway reachable[/green] — env={h.get('environment')} mock={h.get('mock_anthropic')}")
        caps = h.get("capabilities") or {}
        active = [k for k, v in caps.items() if isinstance(v, dict) and v.get("active")]
        if active:
            print(f"  capabilities: [cyan]{', '.join(active)}[/cyan]")
    except DecomputeError as exc:
        print(f"[yellow]Config saved, but the gateway isn't reachable yet:[/yellow] {exc}")
        print("Start it (scripts/run_gateway.sh), then re-run [bold]decompute init[/bold] or [bold]decompute doctor[/bold].")
    # Finish the job: leave Claude Code ready to route through the gateway.
    wire = yes or typer.confirm("Configure Claude Code to route through the gateway now?", default=True)
    if wire:
        env_path = _write_claude_env(base_url, api_key)
        print(f"[green]✓[/green] Claude Code env written: {env_path}")
        print(f"  Activate: [bold]source {env_path}[/bold]   then run [bold]claude[/bold]")
        print(f"  Permanent: [bold]decompute claude --write-profile[/bold]   (or [bold]decompute vscode .[/bold] for VS Code)")
    else:
        print("Next: [bold]decompute claude[/bold] (or [bold]decompute vscode .[/bold]) when you're ready.")


@app.command()
def claude(
    write_profile: bool = typer.Option(False, "--write-profile", help="append the source line to your shell rc"),
):
    """Configure Claude Code to route through the Decompute gateway (writes a sourceable env file)."""
    base_url, api_key = _conn()
    env_path = _write_claude_env(base_url, api_key)
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
def doctor(json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON (used by `support bundle`).")):
    """Diagnose the gateway connection, auth, and capabilities."""
    client = _client()
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

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

    ok_all = all(c["ok"] for c in checks)
    if json_output:
        print(json.dumps({"ok": ok_all, "checks": checks}, indent=2))
    else:
        table = Table(title="decompute doctor", show_header=True, header_style="bold")
        table.add_column("check"); table.add_column("status"); table.add_column("detail")
        for c in checks:
            table.add_row(str(c["check"]), "[green]ok[/green]" if c["ok"] else "[red]FAIL[/red]", str(c["detail"]))
        print(table)
    raise typer.Exit(code=0 if ok_all else 1)


support_app = typer.Typer(help="Support / debugging utilities.")
app.add_typer(support_app, name="support")


@support_app.command("bundle")
def support_bundle(
    session: Optional[str] = typer.Option(None, "--session", help="Scope to one Claude Code session id (x-decompute-session-id)."),
    since: Optional[str] = typer.Option(None, "--since", help="Session activity since this time (ISO 8601, unix epoch, or '2h'/'7d')."),
    until: Optional[str] = typer.Option(None, "--until", help="Session activity up to this time."),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", help="Where to write the zip (default: <data-dir>/support)."),
    data_dir: str = typer.Option(".decompute", "--data-dir", help="Gateway data dir holding logs/config."),
    project_root: str = typer.Option(".", "--project-root"),
    pii_mode: str = typer.Option("tokenize", "--pii-mode", help="tokenize | redact"),
    release_version: str = typer.Option("v0.1.0-beta.1", "--release-version"),
    canary: list[str] = typer.Option([], "--canary", help="A fake secret/prompt that must NOT appear in the bundle (repeatable)."),
):
    """Create a privacy-scrubbed support bundle — no raw prompts, CCR payloads, secrets, or PII.

    With --session, folds in a metadata-only report for that Claude Code session (request
    IDs, models, statuses, token/latency summaries — never prompt content).
    """
    try:
        from decompute_gateway.support_bundle import build_support_bundle
    except Exception:
        print("[red]decompute_gateway.support_bundle is not importable[/red] — run this from the gateway environment.")
        raise typer.Exit(code=1)

    session_report = None
    if session:
        params = {"session_id": session}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        try:
            session_report = _client().get("/v1/support/session", params=params)
            n = (session_report or {}).get("summary", {}).get("requests", 0)
            print(f"[green]✓[/green] session report: {n} request(s) for [bold]{session}[/bold]")
        except DecomputeError as exc:
            print(f"[yellow]⚠ couldn't fetch the session report ({exc}); building the bundle without it.[/yellow]")

    data = Path(data_dir).expanduser()
    out = Path(output_dir).expanduser() if output_dir else data / "support"
    result = build_support_bundle(
        output_dir=out,
        data_dir=data,
        project_root=Path(project_root),
        pii_mode=pii_mode,
        release_version=release_version,
        extra_canaries=list(canary),
        session_report=session_report,
    )
    ok = bool(result.leak_scan.get("ok"))
    print("Support bundle created:")
    print(f"  {result.path}")
    print()
    if session:
        print(f"Session scope: {session}" + (f"  since {since}" if since else ""))
    print("Raw prompts included: no")
    print("CCR payloads included: no")
    print("Secrets scrubbed: yes")
    print(f"PII mode: {pii_mode}")
    print(f"Leak scan: {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}")
    if not ok:
        print(json.dumps(result.leak_scan, indent=2, sort_keys=True))
        raise typer.Exit(code=2)


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
