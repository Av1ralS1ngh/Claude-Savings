"""DEVELOPER-ONLY bare-subprocess test runner. NOT FOR PRODUCTION OR THE REQUEST PATH.

The plan permits a bare `subprocess` runner ONLY in a developer-only helper that is not imported by
the FastAPI app and is not reachable from request handling (plan §"Layer 1"). This module is exactly
that: it runs code in a plain subprocess with no isolation. It must never be imported by the gateway
package's request code — a security test asserts that.

Use it only for local calibration when you have no container runtime, and only on code you trust.
"""
from __future__ import annotations

import asyncio
import sys

from .sandbox import build_python_script
from .verifier import TestRunResult


async def run_unsandboxed(*, code: str, tests: str, timeout_s: int = 10) -> TestRunResult:
    """Run student code + tests in a bare subprocess. NO isolation. Dev/calibration only."""
    script = build_python_script(code=code, tests=tests)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=script.encode()), timeout=timeout_s)
    except asyncio.TimeoutError:
        return TestRunResult(ran=True, passed=False, backend="dev_unsandboxed", detail="timeout")
    passed = proc.returncode == 0 and b"__TS_TESTS_OK__" in out
    return TestRunResult(ran=True, passed=passed, backend="dev_unsandboxed", detail=None if passed else err.decode("utf-8", "replace")[-500:])
