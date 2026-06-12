from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from decompute_gateway.config import Settings
from decompute_gateway.crypto import CCRCrypto
from decompute_gateway.db import Store
from decompute_gateway.secret_scanner import SecretScanner
import decompute_gateway.yscompress as ys
from decompute_gateway.yscompress import YSCompressClient, release_large_cache_control_blocks
from decompute_gateway.workload import classify_workload


@dataclass
class _FakeHeadroomResult:
    messages: list[dict[str, Any]]
    transforms_applied: list[str]


def _client(tmp_path) -> YSCompressClient:
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'cmp.db'}")
    store = Store(settings)
    store.create_schema_for_local_dev()
    return YSCompressClient(settings, store, CCRCrypto(None, "local-v1"), SecretScanner())


def _html(chars: int = 20000) -> str:
    body = "".join(
        f"<section><h2>Feature {i}</h2><p>Decompute compresses attached HTML pages for Claude Code.</p></section>"
        for i in range(chars // 120)
    )
    return f"<!DOCTYPE html><html><head><title>Demo</title><style>body{{color:red}}</style></head><body>{body}</body></html>"


def test_release_large_cache_control_blocks_only_releases_large_blocks():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this file about?", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": _html(), "cache_control": {"type": "ephemeral"}},
        ],
    }]

    released_messages, released = release_large_cache_control_blocks(messages)

    assert released == 1
    assert "cache_control" in released_messages[0]["content"][0]
    assert "cache_control" not in released_messages[0]["content"][1]
    assert "cache_control" in messages[0]["content"][1]


def test_cache_control_html_attachment_is_allowed_to_compress(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def fake_compress(messages, **kwargs):
        seen["messages"] = messages
        out = [{
            "role": "user",
            "content": [
                messages[0]["content"][0],
                {"type": "text", "text": "HTML summary: a Decompute product page."},
            ],
        }]
        return _FakeHeadroomResult(out, ["router:text_block:html"])

    monkeypatch.setattr(ys, "_HEADROOM_AVAILABLE", True)
    monkeypatch.setattr(ys, "_headroom_compress", fake_compress)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this file about?", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": _html(), "cache_control": {"type": "ephemeral"}},
        ],
    }]

    result = asyncio.run(_client(tmp_path).compress_messages(
        messages,
        project_id="proj_default",
        compress_cache_control_large_blocks=True,
    ))

    assert result.tokens_saved > result.tokens_before * 0.9
    assert result.engine == "yscompress_rust"
    assert "cache_control:large_block_released:1" in result.transforms_applied
    assert "cache_control" not in seen["messages"][0]["content"][1]
    assert result.messages[0]["content"][0]["text"] == "What is this file about?"
    assert result.messages[0]["content"][1]["text"].startswith("HTML summary")


def test_cache_control_metadata_only_gain_returns_original(monkeypatch, tmp_path):
    def fake_noop(messages, **kwargs):
        return _FakeHeadroomResult(messages, ["router:noop"])

    monkeypatch.setattr(ys, "_HEADROOM_AVAILABLE", True)
    monkeypatch.setattr(ys, "_headroom_compress", fake_noop)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this file about?"},
            {"type": "text", "text": _html(), "cache_control": {"type": "ephemeral"}},
        ],
    }]

    result = asyncio.run(_client(tmp_path).compress_messages(
        messages,
        project_id="proj_default",
        compress_cache_control_large_blocks=True,
    ))

    assert result.tokens_saved == 0
    assert result.engine.endswith(":passthrough_no_gain")
    assert "cache_control" in result.messages[0]["content"][1]


def test_html_attachment_classifies_as_html():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this file about?"},
            {"type": "text", "text": _html()},
        ],
    }]

    assert classify_workload(messages) == "html"


def test_system_html_attachment_is_compressed_without_touching_instruction(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def fake_compress(messages, **kwargs):
        seen["messages"] = messages
        out = [{"role": "user", "content": [{"type": "text", "text": "HTML summary: product docs."}]}]
        return _FakeHeadroomResult(out, ["router:text_block:html"])

    monkeypatch.setattr(ys, "_HEADROOM_AVAILABLE", True)
    monkeypatch.setattr(ys, "_headroom_compress", fake_compress)
    system = [
        {"type": "text", "text": "You are Claude Code. Follow the developer instructions exactly.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _html(), "cache_control": {"type": "ephemeral"}},
    ]

    new_system, before, after, transforms = asyncio.run(
        _client(tmp_path).compress_system(system)
    )

    assert before > after * 10
    assert transforms == ["system:router:text_block:html"]
    assert new_system[0] == system[0]
    assert new_system[1]["text"].startswith("HTML summary")
    assert "cache_control" not in new_system[1]
    assert seen["messages"][0]["content"][0]["text"].startswith("<!DOCTYPE html>")
