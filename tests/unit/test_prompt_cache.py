"""Regression: the gateway must not override a client's own cache_control.

Claude Code marks blocks with cache_control (e.g. ttl='1h'). In `auto` mode the gateway used to
ALSO add a top-level cache_control (ttl 5m), and Anthropic 400s with "Top-level cache_control has
ttl='5m' but the target block already has cache_control with ttl='1h'". When the client already
manages caching anywhere, the gateway now passes through untouched.
"""
from decompute_gateway.anthropic_provider import maybe_apply_prompt_cache


def _has_top_level(body):
    return "cache_control" in body


def test_passthrough_when_block_has_cache_control():
    body = {
        "model": "claude-haiku-4-5",
        "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = maybe_apply_prompt_cache(body, "auto")
    assert not _has_top_level(out)  # gateway did NOT add a conflicting top-level cache_control


def test_passthrough_when_tool_or_message_block_has_cache_control():
    for body in (
        {"tools": [{"name": "bash", "cache_control": {"type": "ephemeral", "ttl": "1h"}}], "messages": []},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]},
        {"cache_control": {"type": "ephemeral", "ttl": "1h"}, "messages": []},  # client set top-level itself
    ):
        out = maybe_apply_prompt_cache(body, "auto")
        # never duplicate/override the client's directive
        assert out.get("cache_control", body.get("cache_control")) == body.get("cache_control")


def test_auto_adds_cache_control_only_when_client_set_none():
    body = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]}
    out = maybe_apply_prompt_cache(body, "auto")
    assert out.get("cache_control") == {"type": "ephemeral"}  # naive client -> gateway enables caching


def test_off_and_passthrough_modes_never_add():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert "cache_control" not in maybe_apply_prompt_cache(body, "off")
    assert "cache_control" not in maybe_apply_prompt_cache(body, "passthrough")
