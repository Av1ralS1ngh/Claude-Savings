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
    for cmd in ("status", "start", "stop", "smoke", "install", "pull", "models"):
        assert cmd in result.output


def test_student_models_catalog_is_well_formed():
    """Every catalog entry has the fields the wizard needs, ordered small->large, with valid hf specs."""
    from decompute_ai.cli import STUDENT_MODELS

    assert len(STUDENT_MODELS) >= 4
    sizes = [m["gb"] for m in STUDENT_MODELS]
    assert sizes == sorted(sizes), "catalog must be ordered smallest-first for the recommender"
    ids = [m["id"] for m in STUDENT_MODELS]
    assert len(ids) == len(set(ids)), "model ids must be unique"
    for m in STUDENT_MODELS:
        assert ":" in m["hf"], f"{m['id']} hf spec must be repo:quant"
        assert m["gb"] > 0 and m["min_ram_gb"] >= m["gb"], f"{m['id']} min_ram must exceed weight"
        assert m["note"]


def test_fits_ram_logic():
    from decompute_ai.cli import _fits_ram

    big = {"min_ram_gb": 48}
    small = {"min_ram_gb": 8}
    assert _fits_ram(small, 16) is True
    assert _fits_ram(big, 16) is False
    assert _fits_ram(big, 0) is True  # unknown RAM is permissive (warn, never block)


def test_is_downloaded_matches_real_filename_variants():
    """Reuse-detection must match a manually-downloaded GGUF whose name differs from the hf spec —
    e.g. repo '...-7B-Instruct-GGUF:Q5_K_M' vs file 'qwen2.5-coder-7b-instruct-q5_k_m-00001-of-00002.gguf'."""
    from decompute_ai.cli import _is_downloaded

    model = {"hf": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q5_K_M"}
    cached = ["qwen2.5-coder-7b-instruct-q5_k_m-00001-of-00002.gguf"]
    assert _is_downloaded(model, cached) is True
    # Wrong quant must NOT match.
    assert _is_downloaded(model, ["qwen2.5-coder-7b-instruct-q4_k_m.gguf"]) is False
    # Different model must NOT match.
    assert _is_downloaded(model, ["llama-3.1-8b-instruct-q5_k_m.gguf"]) is False
    assert _is_downloaded(model, []) is False


def test_student_models_help_lists_models_command():
    result = runner.invoke(app, ["student", "models", "--help"])
    assert result.exit_code == 0
    assert "--model" in result.output and "--yes" in result.output


def test_find_local_model_path_picks_split_part_one(tmp_path, monkeypatch):
    """An already-downloaded model must resolve to its on-disk PATH (so `student start` loads it with
    -m instead of re-downloading via -hf). For a split GGUF, part 1 is returned."""
    import decompute_ai.cli as cli

    # A fake models dir with a split Qwen-7B (parts out of order on disk).
    (tmp_path / "qwen2.5-coder-7b-instruct-q5_k_m-00002-of-00002.gguf").write_bytes(b"GGUF")
    (tmp_path / "qwen2.5-coder-7b-instruct-q5_k_m-00001-of-00002.gguf").write_bytes(b"GGUF")
    monkeypatch.setattr(cli, "_model_search_dirs", lambda: [tmp_path])

    model = next(m for m in cli.STUDENT_MODELS if m["id"] == "qwen2.5-coder-7b")
    p = cli._find_local_model_path(model)
    assert p is not None and p.name.endswith("00001-of-00002.gguf")  # part 1, not part 2

    # A model that's NOT on disk resolves to None.
    other = next(m for m in cli.STUDENT_MODELS if m["id"] == "qwen2.5-coder-32b")
    assert cli._find_local_model_path(other) is None


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


def test_student_install_opens_catalog_and_never_downloads_without_consent():
    # `install` is now the RAM-aware model wizard. Answering 'q' at the picker exits with NO download.
    result = runner.invoke(app, ["student", "install"], input="q\n")
    assert result.exit_code == 0
    # The catalog (model names + RAM tiers) is shown, and nothing is downloaded without an explicit pick.
    assert "Qwen2.5-Coder" in result.output
    assert "Min RAM" in result.output


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


def test_explain_ts_error_actionable_messages(capsys):
    """ts-run/ts-health turn raw errors into actionable guidance, not cryptic 'HTTP 404'."""
    from decompute_ai.cli import _explain_ts_error
    from decompute_ai.errors import APIError, DecomputeError

    # 404 -> "feature not available" guidance
    _explain_ts_error(APIError(404, "HTTP 404: Not Found"))
    out = capsys.readouterr().out.lower()
    assert "teacher-student feature isn't available" in out or "feature" in out

    # connection failure -> "can't reach the gateway"
    _explain_ts_error(DecomputeError("connection to http://127.0.0.1:9999 failed: refused"))
    out = capsys.readouterr().out.lower()
    assert "can't reach the gateway" in out

    # 503 -> student not ready
    _explain_ts_error(APIError(503, "student_unavailable"))
    out = capsys.readouterr().out.lower()
    assert "student isn't ready" in out or "student start" in out


def test_commands_cheatsheet_lists_key_commands():
    """`decompute commands` shows a grouped cheat-sheet covering the key user commands."""
    result = runner.invoke(app, ["commands"])
    assert result.exit_code == 0
    for c in ("init", "claude", "student models", "ts-run", "status", "dashboard"):
        assert c in result.output, f"cheat-sheet missing {c!r}"
    # groups present
    assert "Getting started" in result.output


def test_commands_plain_is_deduped_scriptable():
    result = runner.invoke(app, ["commands", "--plain"])
    assert result.exit_code == 0
    lines = [l for l in result.output.splitlines() if l.startswith("decompute ")]
    assert "decompute init" in lines
    # 'student models' + 'student start' must collapse to a single 'decompute student'
    assert lines.count("decompute student") == 1


def test_student_endpoint_loopback_no_consent(tmp_path, monkeypatch):
    """A loopback endpoint is trusted — no consent prompt — and persists as openai_endpoint."""
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(tmp_path))
    import decompute_ai.config as cfg
    import importlib; importlib.reload(cfg)
    import decompute_ai.cli as cli; importlib.reload(cli)
    from typer.testing import CliRunner
    r = CliRunner().invoke(cli.app, ["student", "endpoint", "http://127.0.0.1:18080/v1", "--model", "qwen"])
    assert r.exit_code == 0, r.output
    assert "endpoint set" in r.output.lower()
    student = cfg.load_config().get("student", {})
    assert student.get("backend") == "openai_endpoint"
    assert student.get("base_url") == "http://127.0.0.1:18080/v1"
    importlib.reload(cfg); importlib.reload(cli)


def test_student_endpoint_public_requires_consent(tmp_path, monkeypatch):
    """A public third-party endpoint warns + aborts on 'n', changing nothing."""
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(tmp_path))
    import decompute_ai.config as cfg
    import importlib; importlib.reload(cfg)
    import decompute_ai.cli as cli; importlib.reload(cli)
    from typer.testing import CliRunner
    r = CliRunner().invoke(cli.app, ["student", "endpoint", "https://api.host.xyz/v1", "--model", "big"], input="n\n")
    assert r.exit_code == 0
    assert "third-party" in r.output.lower() and "aborted" in r.output.lower()
    assert cfg.load_config().get("student") is None  # nothing saved
    importlib.reload(cfg); importlib.reload(cli)


def test_endpoint_host_tier_classification():
    from decompute_ai.cli import _endpoint_host_tier
    assert _endpoint_host_tier("http://127.0.0.1:8080/v1") == "loopback"
    assert _endpoint_host_tier("http://localhost/v1") == "loopback"
    assert _endpoint_host_tier("http://192.168.1.5/v1") == "private"
    assert _endpoint_host_tier("http://100.101.1.1/v1") == "private"   # tailscale
    assert _endpoint_host_tier("https://api.together.xyz/v1") == "public"
    assert _endpoint_host_tier("https://1.1.1.1/v1") == "public"


def test_student_endpoint_key_stored_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPUTE_CONFIG_DIR", str(tmp_path))
    import decompute_ai.config as cfg
    import importlib; importlib.reload(cfg)
    cfg.save_student_endpoint_key("sk-secret-xyz")
    import os, stat
    mode = stat.S_IMODE(os.stat(cfg.STUDENT_ENDPOINT_KEY_FILE).st_mode)
    assert mode == 0o600, oct(mode)
    assert cfg.load_student_endpoint_key() == "sk-secret-xyz"
    importlib.reload(cfg)


def test_ts_eval_summarizes_accept_rate(monkeypatch, tmp_path):
    """ts-eval runs the task set through the client and reports local accept-rate + latency, and
    appends a comparable JSON result line."""
    import decompute_ai.cli as cli

    calls = {"n": 0}

    class _FakeTS:
        def run(self, task, **k):
            calls["n"] += 1
            # First two served locally, rest escalate — exercises both branches.
            route = "student_only" if calls["n"] <= 2 else "teacher_corrected"
            return {"final_answer": "x", "route": route, "verdict": {"confidence": 0.85}, "usage": {}}

    class _FakeClient:
        teacher_student = _FakeTS()

    monkeypatch.setattr(cli, "_client", lambda: _FakeClient())
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("a[i] bug fix\nreverse a string\nmerge lists\n")  # includes brackets (markup-escape test)
    out = tmp_path / "res.jsonl"
    from typer.testing import CliRunner
    r = CliRunner().invoke(cli.app, ["ts-eval", "--tasks-file", str(tasks), "--label", "t", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "local accept-rate" in r.output
    import json
    line = json.loads(out.read_text().strip().splitlines()[-1])
    assert line["n"] == 3 and line["accepted_local"] == 2 and line["escalated_to_claude"] == 1
    assert line["label"] == "t"
