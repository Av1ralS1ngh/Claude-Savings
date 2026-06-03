"""Regression: the gateway must forward Anthropic `anthropic-beta` headers by default.

Claude Code opts into beta features (context_management, 1M context, fine-grained tool
streaming, interleaved thinking, prompt-caching) via the `anthropic-beta` header AND a
matching request-body field. The gateway used to intersect the header against an allowlist
that defaults to EMPTY, so it stripped every beta — and Anthropic then rejected the body it
gates with 400 "context_management: Extra inputs are not permitted". Default is now
passthrough; the allowlist is an opt-in restriction.
"""
from __future__ import annotations

from decompute_gateway.anthropic_provider import upstream_headers
from decompute_gateway.config import Settings

BETAS = "context-management-2025-06-27,context-1m-2025-08-07,fine-grained-tool-streaming-2025-05-14"


def _set(settings: Settings, **kw):
    for k, v in kw.items():
        try:
            setattr(settings, k, v)
        except Exception:
            object.__setattr__(settings, k, v)
    return settings


def test_beta_forwarded_verbatim_by_default():
    s = Settings()  # empty allowlist == allow all
    assert s.allowed_anthropic_beta_headers == []
    h = upstream_headers(s, "sk-ant-x", {"anthropic-beta": BETAS})
    assert h["anthropic-beta"] == BETAS


def test_allowlist_restricts_when_explicitly_set():
    s = _set(Settings(), allowed_anthropic_beta_headers=["context-1m-2025-08-07"])
    h = upstream_headers(s, "sk-ant-x", {"anthropic-beta": BETAS})
    assert h["anthropic-beta"] == "context-1m-2025-08-07"  # only the allowed one survives


def test_no_beta_header_when_client_sends_none():
    h = upstream_headers(Settings(), "sk-ant-x", {})
    assert "anthropic-beta" not in h
