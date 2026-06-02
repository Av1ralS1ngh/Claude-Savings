from __future__ import annotations

import json
import os
import shutil
import uuid
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


def _new_session_id() -> str:
    """A fresh, opaque working-session id (safe charset, well under the gateway's 64-char cap)."""
    return f"cc-{uuid.uuid4().hex[:20]}"


def _claude_env_lines(base_url: str, api_key: str, session_id: str) -> dict[str, str]:
    """The env Claude Code needs: gateway routing, auth, and the session-id custom header.

    ANTHROPIC_CUSTOM_HEADERS makes Claude Code stamp x-decompute-session-id on every upstream
    request (main model + background + token-count), which the gateway stores so support
    bundles can be scoped to one session. Format is `Name: Value` (Claude Code env reference).
    """
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_CUSTOM_HEADERS": f"x-decompute-session-id: {session_id}",
    }


def _write_claude_env(base_url: str, api_key: str, session_id: str) -> Path:
    """Write the sourceable Claude Code env file. Returns its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = CONFIG_DIR / "claude.env"
    env_path.write_text(
        "".join(f'export {k}="{v}"\n' for k, v in _claude_env_lines(base_url, api_key, session_id).items()),
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
        print("This is your [bold]Decompute[/bold] gateway URL and key — [dim]not[/dim] your Anthropic key.")
        print("Get your key at [bold]https://decompute.ai[/bold] after signup, or use [bold]dc_dev_local[/bold] for local dev.\n")
        base_url = typer.prompt("Gateway URL", default=base_url)
        api_key  = typer.prompt("Decompute API key  (starts with dc_)", default=api_key)
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        print(f"[red]✗[/red] Gateway URL must start with http:// or https://  (got: {base_url!r})")
        raise typer.Exit(1)
    if not api_key.startswith("dc_"):
        print("[yellow]⚠[/yellow] Decompute keys start with 'dc_' — you may have entered your Anthropic key by mistake.")
        if not typer.confirm("Continue anyway?", default=False):
            raise typer.Exit(1)
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
        sid = _new_session_id()
        env_path = _write_claude_env(base_url, api_key, sid)
        save_config({"last_session_id": sid})
        print(f"[green]✓[/green] Claude Code env written: {env_path}")
        print(f"  Session id: [cyan]{sid}[/cyan]")
        print(f"  Activate: [bold]source {env_path}[/bold]   then run [bold]claude[/bold]")
        print(f"  Permanent: [bold]decompute claude --write-profile[/bold]   (or [bold]decompute vscode .[/bold] for VS Code)")
    else:
        print("Next: [bold]decompute claude[/bold] (or [bold]decompute vscode .[/bold]) when you're ready.")


@app.command()
def claude(
    write_profile: bool = typer.Option(False, "--write-profile", help="append the source line to your shell rc"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="reuse a specific session id (default: mint a fresh one)"),
):
    """Configure Claude Code to route through the Decompute gateway (writes a sourceable env file).

    Each run mints a fresh x-decompute-session-id (via ANTHROPIC_CUSTOM_HEADERS) so the
    working session can be scoped in a support bundle; reuse one with --session-id.
    """
    base_url, api_key = _conn()
    sid = session_id or _new_session_id()
    env_path = _write_claude_env(base_url, api_key, sid)
    save_config({"last_session_id": sid})
    print(f"[green]✓[/green] wrote {env_path}")
    print(f"  Session id: [cyan]{sid}[/cyan]   (support: [bold]decompute support bundle[/bold])")
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
    session_id: Optional[str] = typer.Option(None, "--session-id", help="reuse a specific session id (default: mint a fresh one)"),
):
    """Configure a VS Code project so Claude Code in its terminal routes through the gateway."""
    base_url, api_key = _conn()
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
    env_block = _claude_env_lines(base_url, api_key, sid)
    for key in ("terminal.integrated.env.osx", "terminal.integrated.env.linux", "terminal.integrated.env.windows"):
        block = settings.get(key)
        if not isinstance(block, dict):
            block = {}
        block.update(env_block)
        settings[key] = block
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    save_config({"last_session_id": sid})
    print(f"[green]✓[/green] configured {settings_path}")
    print(f"  Session id: [cyan]{sid}[/cyan]")
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


@app.command()
def status():
    """One-glance health: config, routing, gateway, and your savings so far."""
    base_url, api_key = _conn()
    cfg = load_config()
    routed = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") == base_url.rstrip("/")
    t = Table(title="decompute status", show_header=False)
    t.add_column("k", style="bold"); t.add_column("v")
    t.add_row("gateway", base_url)
    t.add_row("api key", (api_key[:10] + "…") if api_key else "[red]missing[/red]")
    t.add_row("session id", cfg.get("last_session_id") or "[dim]none — run `decompute claude`[/dim]")
    t.add_row("Claude Code routed (this shell)",
              "[green]yes[/green]" if routed else "[yellow]no — `source ~/.decompute/claude.env`[/yellow]")
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
        u = client.usage.summary()
        t.add_row("usage", f"{u.get('requests', 0)} requests · {u.get('tokens_saved', 0):,} tokens saved · "
                           f"${u.get('estimated_savings_usd', 0.0):.4f} saved")
    except DecomputeError:
        pass
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
def limits():
    """Show your quota (rate limits + monthly spend cap) and current consumption."""
    try:
        d = _client().get("/v1/limits")
    except DecomputeError as exc:
        print(f"[red]could not fetch limits:[/red] {exc}")
        raise typer.Exit(1)
    lim = d.get("limits", {})
    t = Table(title=f"limits — {d.get('project_id', '')}")
    t.add_column("quota"); t.add_column("value")
    t.add_row("requests / min (per key)", str(lim.get("per_key_rpm")))
    t.add_row("tokens / min (project)", str(lim.get("project_tokens_per_min")))
    t.add_row("max context tokens", str(lim.get("max_context_tokens")))
    cap = d.get("monthly_spend_cap_usd")
    t.add_row("monthly spend", f"${d.get('monthly_spend_usd', 0.0):.4f}" + (f" / ${cap} cap" if cap is not None else ""))
    print(t)


keys_app = typer.Typer(help="Manage your API keys.")
app.add_typer(keys_app, name="keys")


@keys_app.command("list")
def keys_list():
    """List your project's API keys (revoked ones included)."""
    rows = _client().api_keys.list()
    if not rows:
        print("No API keys yet — create one with [bold]decompute api-key-create[/bold].")
        return
    t = Table(title="api keys")
    for c in ("id", "name", "key_prefix", "scopes", "created", "revoked"):
        t.add_column(c)
    for r in rows:
        t.add_row(str(r.get("id", "")), str(r.get("name", "")), str(r.get("key_prefix", "")),
                  ",".join(r.get("scopes") or []), str(r.get("created_at", ""))[:10],
                  "[red]revoked[/red]" if r.get("revoked_at") else "active")
    print(t)


@keys_app.command("revoke")
def keys_revoke(
    key_id: str = typer.Argument(..., help="key id from `decompute keys list`"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
):
    """Revoke an API key (irreversible). Use this immediately if a key leaks."""
    if not yes and not typer.confirm(f"Revoke key {key_id}? This cannot be undone."):
        raise typer.Exit(1)
    try:
        _client().api_keys.revoke(key_id)
    except DecomputeError as exc:
        print(f"[red]revoke failed:[/red] {exc}")
        raise typer.Exit(1)
    print(f"[green]✓[/green] revoked {key_id}")


support_app = typer.Typer(help="Support / debugging utilities.")
app.add_typer(support_app, name="support")


def _upload_support_bundle(zip_path: Path) -> dict:
    """POST the bundle zip to the gateway's support inbox; returns the server's response."""
    import httpx
    base_url, api_key = _conn()
    resp = httpx.post(
        f"{base_url}/v1/support/upload",
        content=zip_path.read_bytes(),
        headers={"x-decompute-api-key": api_key, "content-type": "application/zip"},
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
        session = load_config().get("last_session_id")
        if session:
            print(f"[dim]Scoping to your last session {session} — use --session to override, --all for everything.[/dim]")

    base_url, api_key = _conn()
    headers = {"x-decompute-api-key": api_key}

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


@app.command("api-key-create")
def api_key_create(name: str = "default"):
    print_json(_client().api_keys.create(name=name))


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()
