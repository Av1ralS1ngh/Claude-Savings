from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import print
from rich.table import Table

from .client import Decompute
from .config import CONFIG_PATH, load_config, save_config
from .errors import DecomputeError

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
