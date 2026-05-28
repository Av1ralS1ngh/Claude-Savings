from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import print
from rich.table import Table

from .client import Decompute

app = typer.Typer(help="Decompute Gateway CLI")


def _client() -> Decompute:
    return Decompute()


@app.command()
def health():
    c = _client()
    print(c.get("/health"))


@app.command("devices-scan")
def devices_scan():
    rows = _client().devices.scan()
    print_json(rows)


@app.command("devices-list")
def devices_list():
    rows = _client().devices.list()
    print_json(rows)


@app.command("compress")
def compress(path: Optional[Path] = None, text: Optional[str] = None, mode: str = "safe"):
    content = text or (path.read_text() if path else typer.prompt("Text"))
    result = _client().compression.preview([{"role": "tool", "content": content}], mode=mode)
    print_json(result)


@app.command("plan")
def plan(model: str = "claude-sonnet-4-5", path: Optional[Path] = None, message: str = "Hello"):
    content = path.read_text() if path else message
    result = _client().routes.plan(model=model, messages=[{"role": "user", "content": content}])
    print_json(result)


@app.command("chat")
def chat(message: str, model: str = "claude-sonnet-4-5"):
    result = _client().chat.completions.create(model=model, messages=[{"role": "user", "content": message}])
    print_json(result)


@app.command("usage")
def usage():
    print_json(_client().get("/v1/usage"))


@app.command("api-key-create")
def api_key_create(name: str = "default"):
    print_json(_client().api_keys.create(name=name))


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()
