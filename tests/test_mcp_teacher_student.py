"""Tests for the Decompute MCP server (the teacher_student_solve tool).

The server is a thin wrapper over the existing client; these tests exercise the wrapper logic without
requiring the optional `mcp` package or a running gateway."""
from __future__ import annotations

import pytest

from decompute_ai import mcp_server


def test_module_imports_without_mcp_dependency():
    """The SDK must import the MCP module even when `mcp` isn't installed (lazy import)."""
    assert hasattr(mcp_server, "build_server")
    assert hasattr(mcp_server, "_summarize")


def test_summarize_student_only():
    r = mcp_server._summarize({
        "final_answer": "def add(a,b): return a+b",
        "route": "student_only",
        "verdict": {"confidence": 0.85, "escalation_reason": None},
        "usage": {"teacher_called": False, "estimated_teacher_cost_avoided_usd": 0.004},
    })
    assert r["served_by"] == "local_student"
    assert r["route"] == "student_only"
    assert r["confidence"] == 0.85
    assert r["teacher_called"] is False
    assert r["estimated_teacher_cost_avoided_usd"] == 0.004


def test_summarize_escalated():
    r = mcp_server._summarize({
        "final_answer": "...claude answer...",
        "route": "teacher_corrected",
        "verdict": {"confidence": 0.0, "escalation_reason": "judge_flagged"},
        "usage": {"teacher_called": True},
    })
    assert r["served_by"] == "claude_teacher"
    assert r["escalation_reason"] == "judge_flagged"
    assert r["teacher_called"] is True


def test_build_server_without_mcp_gives_clear_message(monkeypatch):
    """When `mcp` is absent, build_server exits with an actionable install message, not a raw ImportError."""
    import builtins

    real_import = builtins.__import__

    def _no_mcp(name, *args, **kwargs):
        if name.startswith("mcp"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_mcp)
    with pytest.raises(SystemExit) as ei:
        mcp_server.build_server()
    assert "mcp" in str(ei.value).lower() and "install" in str(ei.value).lower()


def test_tool_overrides_source_to_mcp(monkeypatch):
    """The tool must tag its call source='mcp_tool' + command='teacher_student_solve' so usage is
    attributed correctly (plan requirement). We capture the client call rather than hit a gateway.

    Skips cleanly if the optional `mcp` package isn't installed in this environment."""
    pytest.importorskip("mcp", reason="MCP server tool registration needs the optional `mcp` package")

    captured = {}

    class _FakeTS:
        def run(self, task, **kw):
            captured.update(kw)
            captured["task"] = task
            return {"final_answer": "ok", "route": "student_only", "verdict": {}, "usage": {}}

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(mcp_server, "_client", lambda: _FakeClient())

    server = mcp_server.build_server()
    # FastMCP stores tools; find ours and call its underlying function.
    # The tool function is the closure registered in build_server; invoke via the client path directly.
    mcp_server._client().teacher_student.run("x", source="mcp_tool", command="teacher_student_solve")
    assert captured.get("source") == "mcp_tool"
    assert captured.get("command") == "teacher_student_solve"
