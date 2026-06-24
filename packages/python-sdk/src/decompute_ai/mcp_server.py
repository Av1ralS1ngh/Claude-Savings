"""Decompute MCP server — exposes the local coding student as MCP tools.

A thin wrapper over the existing Decompute client (the same path `decompute ts-run` uses), so an MCP
host (Claude Code, etc.) can call the local student as a tool: it runs student -> verifier -> optional
Claude teacher and returns the verified result. Per docs/teacher_student_plan.md this is command-gated
and tags its calls with source="mcp_tool" so usage is attributed correctly.

Run as a stdio server:  decompute-mcp     (or: python -m decompute_ai.mcp_server)

The `mcp` package is an optional dependency; install with:  pip install "decompute-ai[mcp]"
"""
from __future__ import annotations

import sys
from typing import Any

from .client import Decompute
from .errors import DecomputeError


def _client() -> Decompute:
    # Same construction as the CLI: Decompute() resolves the gateway base URL + key from env/config.
    return Decompute()


def _summarize(resp: dict[str, Any]) -> dict[str, Any]:
    """Reduce a /v1/teacher_student/run response to the fields a tool caller needs."""
    verdict = resp.get("verdict") or {}
    usage = resp.get("usage") or {}
    return {
        "final_answer": resp.get("final_answer"),
        "route": resp.get("route"),
        "served_by": "local_student" if resp.get("route") == "student_only" else "claude_teacher",
        "confidence": verdict.get("confidence"),
        "escalation_reason": verdict.get("escalation_reason"),
        "teacher_called": usage.get("teacher_called", False),
        "estimated_teacher_cost_avoided_usd": usage.get("estimated_teacher_cost_avoided_usd", 0.0),
    }


def build_server():
    """Construct the MCP server with the teacher-student tools. Imports `mcp` lazily so the SDK as a
    whole never hard-depends on it."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without the optional dep
        raise SystemExit(
            "The MCP server needs the `mcp` package. Install it with:\n"
            '  pip install "decompute-ai[mcp]"   (or: pip install mcp)\n'
            f"(import error: {exc})"
        )

    server = FastMCP("decompute")

    @server.tool()
    def teacher_student_solve(
        task: str,
        tests: str | None = None,
        require_teacher: bool = False,
        execute_tests: bool = False,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Solve a coding task with the local student, verified, escalating to Claude if not confident.

        Mirrors `decompute ts-run`. Returns the final answer plus the route and verifier verdict so the
        caller knows whether the local student answered or it escalated to Claude.

        Args:
            task: The coding task (a clear, self-contained instruction).
            tests: Optional assertions/test code; passing tests in the sandbox is the strongest signal.
            require_teacher: Always get Claude review/correction.
            execute_tests: Run the supplied tests in the sandbox (only if server policy allows it).
            max_tokens: Max tokens for the student's answer.
        """
        try:
            resp = _client().teacher_student.run(
                task,
                tests=tests,
                require_teacher=require_teacher,
                execute_tests=execute_tests,
                max_tokens=max_tokens,
                source="mcp_tool",
                command="teacher_student_solve",
            )
        except DecomputeError as exc:
            return {"error": str(exc), "final_answer": None, "route": None}
        return _summarize(resp)

    @server.tool()
    def teacher_student_health() -> dict[str, Any]:
        """Report local-student / teacher readiness (is the student up, the verifier config, sandbox)."""
        try:
            return _client().teacher_student.health()
        except DecomputeError as exc:
            return {"error": str(exc), "enabled": False}

    return server


def main() -> None:
    """Entry point: run the stdio MCP server."""
    server = build_server()
    server.run()  # FastMCP defaults to stdio transport


if __name__ == "__main__":
    sys.exit(main())
