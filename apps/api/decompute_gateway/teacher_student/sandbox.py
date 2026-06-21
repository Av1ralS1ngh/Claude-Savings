"""Sandboxed test execution for the verifier (plan §"Layer 1" / "Phase 4").

Running student-generated code against tests is the strongest correctness signal, but executing
untrusted code is dangerous. This module enforces the plan's safety contract:

  * Default backend is `disabled` — no code runs, and the verifier caps confidence accordingly.
  * `container` runs code in Docker/Podman with NO network, a read-only root, and memory/CPU/PID
    limits, dropped capabilities, and a hard timeout.
  * `cloudflare` is a hosted-sandbox seam (deferred — raises until implemented).
  * There is NO bare-subprocess execution reachable from the request path. A bare-subprocess runner
    exists ONLY in tests/dev (sandbox_dev_runner.py) and is never imported here.

`make_test_runner(settings)` returns a `run_tests_fn` compatible with verifier.verify, or None when
execution is not enabled/available (so the verifier applies the sandbox-unavailable confidence cap).
"""
from __future__ import annotations

import asyncio
import secrets
import shutil
from contextlib import suppress

from ..config import Settings
from .verifier import TestRunResult

# The harness that wraps student code + tests into a single runnable script. Kept minimal and
# language-agnostic-ish (Python-first, matching the verifier's syntax check). The student code and
# the test snippet runs INSIDE harness-controlled code so a malicious student answer can't fake
# success. Two anti-spoofing measures (Codex B2):
#   1. The success token is an UNPREDICTABLE per-run value the student code never sees as a literal —
#      it's only printed by harness code that runs strictly AFTER the tests pass.
#   2. Student code + tests run in an exec() scope wrapped in try/except that catches SystemExit and
#      BaseException, so `raise SystemExit(0)` or `print(<guessed token>)` at the top of the answer
#      cannot short-circuit to a pass. The token is printed only on the genuine, completed pass path.
_PY_HARNESS = '''import sys as _ts_sys

_TS_CODE = {code!r}
_TS_TESTS = {tests!r}
_TS_OK = {token!r}

_ns = {{}}
try:
    exec(compile(_TS_CODE, "<student_code>", "exec"), _ns)
    exec(compile(_TS_TESTS, "<tests>", "exec"), _ns)
except SystemExit as _e:
    # Student code calling sys.exit() must NOT be able to fake a clean pass.
    _ts_sys.stderr.write("student code raised SystemExit: %r\\n" % (_e,))
    _ts_sys.exit(3)
except BaseException as _e:
    import traceback as _tb
    _tb.print_exc()
    _ts_sys.exit(1)
else:
    # Reached only if BOTH the code loaded and every test assertion passed.
    print(_TS_OK)
    _ts_sys.exit(0)
'''


def build_python_script(*, code: str, tests: str, token: str = "__TS_TESTS_OK__") -> str:
    # !r repr-quotes the code/tests/token as Python string literals, so the student's content is DATA
    # inside exec(), never executed at module top level where it could print the token or sys.exit.
    return _PY_HARNESS.format(code=code, tests=tests, token=token)


class SandboxUnavailable(Exception):
    """The configured sandbox backend is not usable on this host."""


# Process-wide cap on concurrent sandbox containers (Orig-4): without it, N concurrent requests spawn
# N containers and can exhaust host resources. Lazily sized from settings on first use.
_sandbox_semaphores: dict[int, asyncio.Semaphore] = {}


def _sandbox_semaphore(max_concurrency: int) -> asyncio.Semaphore:
    n = max(1, max_concurrency)
    sem = _sandbox_semaphores.get(n)
    if sem is None:
        sem = asyncio.Semaphore(n)
        _sandbox_semaphores[n] = sem
    return sem


def _container_cmd(runtime: str, image: str, timeout_s: int, mem: str, cpus: str, pids: int, *, name: str | None = None) -> list[str]:
    """Build a hardened `docker/podman run` argv. Locked down per plan §"Layer 1":
    no network, read-only root, tmpfs /tmp, dropped caps, no new privileges, memory/CPU/PID caps."""
    cmd = [runtime, "run", "--rm"]
    if name:
        cmd += ["--name", name]
    cmd += [
        "--network", "none",            # no network egress from untrusted code
        "--read-only",                  # read-only root filesystem
        "--tmpfs", "/tmp:rw,size=16m,noexec",
        "--memory", mem, "--memory-swap", mem,  # no swap headroom beyond mem
        "--cpus", cpus,
        "--pids-limit", str(pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",        # nobody
        "-i",                            # read the script from stdin
        image,
        "python", "-",
    ]
    return cmd


class ContainerSandbox:
    def __init__(self, settings: Settings):
        self.runtime = getattr(settings, "teacher_student_sandbox_runtime", "docker")
        self.image = getattr(settings, "teacher_student_sandbox_image", "python:3.12-slim")
        self.timeout_s = int(getattr(settings, "teacher_student_sandbox_timeout_s", 15))
        self.mem = getattr(settings, "teacher_student_sandbox_memory", "256m")
        self.cpus = getattr(settings, "teacher_student_sandbox_cpus", "1.0")
        self.pids = int(getattr(settings, "teacher_student_sandbox_pids", 64))
        self.max_concurrency = int(getattr(settings, "teacher_student_sandbox_max_concurrency", 2))

    def available(self) -> bool:
        return shutil.which(self.runtime) is not None

    async def run(self, *, code: str, tests: str) -> TestRunResult:
        if not self.available():
            # Loud, honest: we cannot run -> the verifier will cap confidence and escalate.
            return TestRunResult(ran=False, passed=False, backend="container", detail=f"{self.runtime} not found")
        # Bound concurrent containers process-wide (Orig-4) so a burst can't exhaust the host.
        async with _sandbox_semaphore(self.max_concurrency):
            return await self._run_one(code=code, tests=tests)

    async def _run_one(self, *, code: str, tests: str) -> TestRunResult:
        # Unpredictable per-run success token: the student answer cannot print it to fake a pass (B2).
        token = "__TS_OK_" + secrets.token_hex(16) + "__"
        script = build_python_script(code=code, tests=tests, token=token)
        # --name lets us force-remove the container if `docker run` is killed before --rm fires (B3).
        container_name = "ts_sandbox_" + secrets.token_hex(8)
        cmd = _container_cmd(self.runtime, self.image, self.timeout_s, self.mem, self.cpus, self.pids, name=container_name)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(input=script.encode()), timeout=self.timeout_s + 5)
        except asyncio.TimeoutError:
            # B3: kill the host `docker run` AND force-remove the container so a looping submission
            # can't keep consuming CPU/memory after we've given up on it.
            await self._reap(proc, container_name)
            return TestRunResult(ran=True, passed=False, backend="container", detail="timeout")
        except Exception as exc:  # docker daemon down, image missing, etc. — could not run safely.
            await self._reap(proc, container_name)
            return TestRunResult(ran=False, passed=False, backend="container", detail=f"{type(exc).__name__}")
        # Pass requires a clean exit AND the unpredictable token on stdout. Student code that does
        # `print(<guess>); raise SystemExit(0)` exits 3 (caught SystemExit) and never prints OUR token.
        passed = proc.returncode == 0 and token.encode() in out
        detail = None if passed else (err.decode("utf-8", "replace")[-500:] or "tests failed")
        return TestRunResult(ran=True, passed=passed, backend="container", detail=detail)

    async def _reap(self, proc, container_name: str) -> None:
        """Terminate a stuck `docker run` and force-remove the container it spawned."""
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
        # Belt-and-braces: kill the container even if the client process is gone.
        with suppress(Exception):
            rm = await asyncio.create_subprocess_exec(
                self.runtime, "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm.wait(), timeout=5)


class CloudflareSandbox:
    """Hosted-sandbox seam for the hosted gateway. Deferred — never silently no-ops."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, *, code: str, tests: str) -> TestRunResult:
        raise SandboxUnavailable("cloudflare sandbox backend is not implemented yet")


def make_test_runner(settings: Settings):
    """Return a `run_tests_fn` for verifier.verify, or None if execution is disabled.

    None is the safe default: the verifier then applies the sandbox-unavailable confidence cap, so a
    disabled sandbox cannot make a wrong answer look verified."""
    backend = getattr(settings, "teacher_student_sandbox_backend", "disabled")
    if not getattr(settings, "teacher_student_test_execution_enabled", False):
        return None
    if backend == "disabled":
        return None
    if backend == "container":
        sandbox = ContainerSandbox(settings)

        async def runner(*, code: str, tests: str) -> TestRunResult:
            return await sandbox.run(code=code, tests=tests)

        return runner
    if backend == "cloudflare":
        sandbox = CloudflareSandbox(settings)

        async def runner(*, code: str, tests: str) -> TestRunResult:
            try:
                return await sandbox.run(code=code, tests=tests)
            except SandboxUnavailable as exc:
                return TestRunResult(ran=False, passed=False, backend="cloudflare", detail=str(exc))

        return runner
    # Unknown backend -> treat as unavailable (the verifier caps confidence).
    return None
