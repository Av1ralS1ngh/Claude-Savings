"""CLI tests for the teacher-student + student-runtime commands (Phase 2 + Phase 6).

Uses typer's CliRunner. Gateway/student calls are stubbed so the tests don't need a running server.
"""
from __future__ import annotations

from typer.testing import CliRunner

from decompute_ai.cli import app

runner = CliRunner()


def test_student_subcommands_registered():
    result = runner.invoke(app, ["student", "--help"])
    assert result.exit_code == 0
    for cmd in ("status", "start", "stop", "smoke", "install", "pull"):
        assert cmd in result.output


def test_ts_commands_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ts-run" in result.output
    assert "ts-health" in result.output
    assert "local-chat" in result.output


def test_student_status_when_unreachable(monkeypatch):
    import decompute_ai.cli as cli

    monkeypatch.setattr(cli, "_student_alive", lambda: False)
    result = runner.invoke(app, ["student", "status"])
    assert result.exit_code == 0
    assert "not reachable" in result.output


def test_student_status_when_reachable(monkeypatch):
    import decompute_ai.cli as cli

    monkeypatch.setattr(cli, "_student_alive", lambda: True)
    result = runner.invoke(app, ["student", "status"])
    assert result.exit_code == 0
    assert "reachable" in result.output


def test_student_install_prints_manual_steps_no_download():
    result = runner.invoke(app, ["student", "install"])
    assert result.exit_code == 0
    # The plan forbids auto-download: install is guidance only.
    assert "nothing is downloaded" in result.output.lower()
    assert "16 GB" in result.output  # the RAM warning is surfaced


def test_student_pull_aborts_without_confirmation():
    # Answer "n" to the confirm prompt -> no download.
    result = runner.invoke(app, ["student", "pull", "some/model:Q4"], input="n\n")
    assert "aborted" in result.output.lower()


def test_ts_run_reports_gateway_error(monkeypatch):
    import decompute_ai.cli as cli
    from decompute_ai.errors import DecomputeError

    class _FakeTS:
        def run(self, *a, **k):
            raise DecomputeError("gateway down")

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(cli, "_client", lambda: _FakeClient())
    result = runner.invoke(app, ["ts-run", "write add(a,b)"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_ts_export_writes_records(monkeypatch, tmp_path):
    import decompute_ai.cli as cli

    class _FakeTS:
        def export_sft(self):
            return '{"id":"c1","completion":"def add(a,b): return a+b","label_source":"student"}'

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(cli, "_client", lambda: _FakeClient())
    out = tmp_path / "sft.jsonl"
    result = runner.invoke(app, ["ts-export", "--out", str(out)])
    assert result.exit_code == 0
    assert out.read_text().strip().startswith("{")
    assert "wrote 1 records" in result.output


def test_ts_export_reports_forbidden(monkeypatch):
    import decompute_ai.cli as cli
    from decompute_ai.errors import DecomputeError

    class _FakeTS:
        def export_sft(self):
            raise DecomputeError("export_forbidden")

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(cli, "_client", lambda: _FakeClient())
    result = runner.invoke(app, ["ts-export"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_ts_run_prints_answer_and_route(monkeypatch):
    import decompute_ai.cli as cli

    class _FakeTS:
        def run(self, task, **k):
            return {
                "final_answer": "def add(a,b): return a+b",
                "route": "student_only",
                "verdict": {"decision": "accept_student", "confidence": 0.9, "escalation_reason": None},
                "usage": {"teacher_called": False, "estimated_teacher_cost_avoided_usd": 0.001},
            }

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(cli, "_client", lambda: _FakeClient())
    result = runner.invoke(app, ["ts-run", "write add(a,b)"])
    assert result.exit_code == 0
    assert "def add" in result.output
    assert "student_only" in result.output
