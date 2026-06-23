"""Sandbox + judge security tests (Phase 4).

Asserts the plan's safety contract: default disabled, hardened container argv, no bare-subprocess on
the request path, and a judge that escalates on doubt and is never the student.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pkgutil

import pytest

from decompute_gateway.config import Settings
from decompute_gateway.teacher_student import judge as J
from decompute_gateway.teacher_student import sandbox as SB


# ── make_test_runner gating ─────────────────────────────────────────────────────


def test_runner_none_when_execution_disabled():
    s = Settings(teacher_student_test_execution_enabled=False, teacher_student_sandbox_backend="container")
    assert SB.make_test_runner(s) is None


def test_runner_none_when_backend_disabled():
    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="disabled")
    assert SB.make_test_runner(s) is None


def test_runner_built_for_container_backend():
    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="container")
    assert SB.make_test_runner(s) is not None


def test_runner_none_for_unknown_backend():
    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="lol")
    assert SB.make_test_runner(s) is None


# ── Harness anti-spoofing (B2) — pure, no Docker needed ──────────────────────────


def test_harness_runs_code_and_tests_as_data_not_top_level():
    """The student code is embedded as a string literal inside exec(), so a top-level print/exit in
    the answer can't run at module scope to fake a pass (B2)."""
    script = SB.build_python_script(code="print('hi'); raise SystemExit(0)", tests="assert True", token="__TOK__")
    # The student's code appears repr-quoted (as data), not as a bare top-level statement.
    assert "raise SystemExit(0)" in script  # present...
    assert "exec(compile(_TS_CODE" in script  # ...but executed via exec(), and SystemExit is caught
    assert "except SystemExit" in script


def test_harness_token_only_printed_after_tests():
    script = SB.build_python_script(code="x=1", tests="assert x==1", token="__SECRET_TOK__")
    # The token is PRINTED only in the else-branch (after both exec calls succeed). Its assignment to
    # _TS_OK appears earlier, but the print(...) of it must come after the tests/else.
    assert "print(_TS_OK)" in script
    assert script.index("else:") < script.index("print(_TS_OK)")


# ── Sandbox concurrency cap (Orig-4) ─────────────────────────────────────────────


def test_sandbox_semaphore_is_bounded():
    sem = SB._sandbox_semaphore(2)
    assert sem._value == 2
    # Same cap returns the same semaphore (process-wide).
    assert SB._sandbox_semaphore(2) is sem


# ── Container argv is hardened ──────────────────────────────────────────────────


def test_container_cmd_has_isolation_flags():
    cmd = SB._container_cmd("docker", "python:3.12-slim", 15, "256m", "1.0", 64)
    joined = " ".join(cmd)
    assert "--network none" in joined            # no network
    assert "--read-only" in joined               # read-only root
    assert "--cap-drop ALL" in joined            # dropped capabilities
    assert "--security-opt no-new-privileges" in joined
    assert "--memory 256m" in joined and "--memory-swap 256m" in joined  # no swap headroom
    assert "--pids-limit 64" in joined
    assert "--user 65534:65534" in joined        # nobody
    assert "--rm" in joined                       # ephemeral


def test_container_sandbox_unavailable_does_not_pretend_pass(monkeypatch):
    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="container")
    sb = SB.ContainerSandbox(s)
    monkeypatch.setattr(SB.shutil, "which", lambda name: None)  # no docker on host
    res = asyncio.run(sb.run(code="def add(a,b): return a+b", tests="assert add(2,3)==5"))
    assert res.ran is False and res.passed is False  # honest: could not run


def test_cloudflare_backend_is_not_silently_noop():
    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="cloudflare")
    runner = SB.make_test_runner(s)
    assert runner is not None
    res = asyncio.run(runner(code="x", tests="y"))
    assert res.ran is False  # deferred backend reports "could not run", never a fake pass


# ── No bare subprocess reachable from the app/request path ──────────────────────


def test_no_request_path_module_imports_bare_runner():
    """The dev-only bare-subprocess runner must not be IMPORTED by any gateway request module.

    Parses each module's AST and looks for an actual import of sandbox_dev_runner — a mere docstring
    mention (e.g. sandbox.py explaining where the dev runner lives) is not an import and is fine."""
    import ast

    import decompute_gateway

    def imports_dev_runner(src: str) -> bool:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("sandbox_dev_runner"):
                return True
            if isinstance(node, ast.Import):
                if any(alias.name.endswith("sandbox_dev_runner") for alias in node.names):
                    return True
        return False

    offenders = []
    for mod in pkgutil.walk_packages(decompute_gateway.__path__, prefix="decompute_gateway."):
        name = mod.name
        if name.endswith("sandbox_dev_runner"):
            continue
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            continue
        if not spec or not spec.origin or not spec.origin.endswith(".py"):
            continue
        with open(spec.origin) as f:
            if imports_dev_runner(f.read()):
                offenders.append(name)
    assert offenders == [], f"bare-subprocess runner imported by request-path modules: {offenders}"


def test_main_does_not_import_bare_runner():
    import decompute_gateway.main as main
    import inspect

    assert "sandbox_dev_runner" not in inspect.getsource(main)


# ── Judge: escalate on doubt, never self-judge ──────────────────────────────────


def test_judge_disabled_returns_none():
    s = Settings(teacher_student_judge_enabled=False)
    assert J.make_judge(s, teacher_caller=lambda **k: None) is None


def test_judge_parse_pass():
    r = J.parse_judge_text('{"verdict":"pass","severity":0,"would_ship":true,"reason":"ok"}')
    assert r.passed is True


def test_judge_parse_fail_escalates():
    r = J.parse_judge_text('{"verdict":"fail","severity":3,"would_ship":false,"reason":"off-by-one"}')
    assert r.passed is False and r.severity == 3


def test_judge_parse_uncertain_escalates():
    r = J.parse_judge_text('{"verdict":"uncertain","severity":1,"would_ship":true}')
    assert r.passed is False


def test_judge_parse_would_not_ship_escalates():
    r = J.parse_judge_text('{"verdict":"pass","severity":0,"would_ship":false}')
    assert r.passed is False  # "would not ship" overrides a pass verdict


def test_judge_parse_unparseable_escalates():
    r = J.parse_judge_text("the model rambled without JSON")
    assert r.passed is False and r.severity >= 2


def test_judge_uses_teacher_not_student():
    s = Settings(teacher_student_judge_enabled=True)
    captured = {}

    class _R:
        text = '{"verdict":"pass","severity":0,"would_ship":true}'

    async def teacher_caller(*, messages, system):
        captured["system"] = system
        return _R()

    judge_fn = J.make_judge(s, teacher_caller=teacher_caller)
    asyncio.run(judge_fn(task_text="add", answer="```python\ndef add(a,b): return a+b\n```", tests=None))
    # The judge prompt asks to FIND FLAWS — it is the Claude teacher path, not the local student.
    assert "FIND FLAWS" in captured["system"]


def test_assert_judge_distinct_from_student():
    with pytest.raises(ValueError):
        J.assert_judge_distinct_from_student(judge_model="local-student-coder", student_model="local-student-coder")
    # Distinct models are fine.
    J.assert_judge_distinct_from_student(judge_model="claude-sonnet", student_model="local-student-coder")


# ── Integration: verifier accepts when sandbox tests pass ───────────────────────


def test_verifier_with_container_runner_accepts_on_pass(monkeypatch):
    from decompute_gateway.teacher_student import verifier as V

    s = Settings(teacher_student_test_execution_enabled=True, teacher_student_sandbox_backend="container")
    sb = SB.ContainerSandbox(s)
    # Pretend docker exists and the run passes.
    monkeypatch.setattr(SB.shutil, "which", lambda name: "/usr/bin/docker")

    async def fake_run(*, code, tests):
        return V.TestRunResult(ran=True, passed=True, backend="container")

    monkeypatch.setattr(sb, "run", fake_run)
    runner = sb.run
    r = asyncio.run(V.verify(task_text="add", answer="```python\ndef add(a,b): return a+b\n```", tests="assert add(2,3)==5", threshold=0.82, run_tests_fn=runner))
    assert not r.escalated and r.tests_passed
