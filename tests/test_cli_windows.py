"""Windows cross-platform branches of the CLI gateway lifecycle (Phase 0/1 of Windows support).

These run on a Mac by injecting sys.platform == 'win32' (monkeypatching the module's _IS_WINDOWS and
the platform-derived constants) and mocking subprocess. They assert that:
  - the win32 branch builds the correct command/flags (DETACHED_PROCESS, taskkill, subprocess.run)
  - the POSIX branch is UNCHANGED (start_new_session, lsof/pkill, os.execve)
A real Windows runner is still needed to confirm the spawned process actually detaches/stops; this
covers the branch-selection + command-construction logic at the right altitude for a mac-only session.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def cli(monkeypatch):
    """Fresh cli module with ~/.decompute redirected, so config writes don't touch the real home."""
    import decompute_ai.config as config
    importlib.reload(config)
    mod = importlib.reload(importlib.import_module("decompute_ai.cli"))
    return mod


def _force_windows(monkeypatch, cli):
    """Flip the module's platform constants to their Windows values, as if imported on win32."""
    monkeypatch.setattr(cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(cli, "_GATEWAY_EXE", "decompute-gateway.exe")
    monkeypatch.setattr(cli, "_VENV_BINDIR", "Scripts")


# ── _stop_gateway: win32 uses netstat + taskkill, never lsof/pkill ──────────────

def test_stop_gateway_windows_parses_netstat_and_taskkills(monkeypatch, cli):
    _force_windows(monkeypatch, cli)
    calls: list[list[str]] = []
    netstat_out = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       4242\n"
        "  TCP    127.0.0.1:55000        127.0.0.1:8080        ESTABLISHED     9999\n"  # client, NOT killed
        "  TCP    0.0.0.0:443            0.0.0.0:0             LISTENING       1234\n"   # other port
    )

    class _Res:
        stdout = netstat_out
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append(args)
        return _Res()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    # os.kill must NOT be used on Windows
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: pytest.fail("os.kill must not be called on Windows"))

    assert cli._stop_gateway("http://127.0.0.1:8080") is True
    # exactly one taskkill, for the LISTENING pid on :8080 (4242) — not the client (9999) or other port (1234)
    taskkills = [c for c in calls if c[0] == "taskkill"]
    assert len(taskkills) == 1
    assert "4242" in taskkills[0]
    assert "/F" in taskkills[0]
    assert not any(c[0] in ("lsof", "pkill") for c in calls), "POSIX tools must not run on Windows"


def test_stop_gateway_posix_still_uses_lsof(monkeypatch, cli):
    """Regression guard: the POSIX path is byte-for-byte the old behavior (lsof + SIGTERM)."""
    assert cli._IS_WINDOWS is False  # this test process is a mac
    seen: list[list[str]] = []

    class _Res:
        stdout = "4242\n"
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: (seen.append(args), _Res())[1])
    sigterms: list[int] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: sigterms.append(pid))

    assert cli._stop_gateway("http://127.0.0.1:8080") is True
    assert seen and seen[0][0] == "lsof"
    assert sigterms == [4242]


# ── _start_gateway: win32 uses creationflags, POSIX uses start_new_session ──────

def test_start_gateway_windows_uses_detached_creationflags(monkeypatch, cli):
    _force_windows(monkeypatch, cli)
    monkeypatch.setattr(cli, "_find_gateway_launcher", lambda: ["C:\\dc\\venv\\Scripts\\decompute-gateway.exe"])
    monkeypatch.setattr(cli, "_gateway_running", lambda *a, **k: True)  # report up immediately
    captured: dict = {}

    class _P:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(cli.subprocess, "Popen", _P)
    # Provide the Windows creationflags constants (absent on a mac's subprocess module).
    monkeypatch.setattr(cli.subprocess, "DETACHED_PROCESS", 0x08, raising=False)
    monkeypatch.setattr(cli.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)

    assert cli._start_gateway("http://127.0.0.1:8080") is True
    kw = captured["kwargs"]
    assert "creationflags" in kw and kw["creationflags"] == (0x08 | 0x200)
    assert "start_new_session" not in kw  # POSIX-only flag must be absent on Windows


def test_start_gateway_posix_uses_start_new_session(monkeypatch, cli):
    assert cli._IS_WINDOWS is False
    monkeypatch.setattr(cli, "_find_gateway_launcher", lambda: ["/dc/venv/bin/decompute-gateway"])
    monkeypatch.setattr(cli, "_gateway_running", lambda *a, **k: True)
    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: captured.update(kwargs))

    assert cli._start_gateway("http://127.0.0.1:8080") is True
    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured


# ── _find_gateway_launcher: Scripts/.exe on win32, bin on POSIX ─────────────────

def test_find_gateway_launcher_windows_probes_scripts_exe(monkeypatch, cli, tmp_path):
    _force_windows(monkeypatch, cli)
    monkeypatch.delenv("DECOMPUTE_GATEWAY_CMD", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)  # nothing on PATH -> dir probe
    home = tmp_path
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("DECOMPUTE_HOME", str(home / ".decompute"))
    exe = home / ".decompute" / "venv" / "Scripts" / "decompute-gateway.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")

    launcher = cli._find_gateway_launcher()
    assert launcher is not None
    assert launcher[0].endswith("decompute-gateway.exe")
    assert "Scripts" in launcher[0]


def test_find_gateway_launcher_windows_never_falls_back_to_run_gateway_sh(monkeypatch, cli, tmp_path):
    """On Windows there is no bash run_gateway.sh fallback — return None when nothing is installed."""
    _force_windows(monkeypatch, cli)
    monkeypatch.delenv("DECOMPUTE_GATEWAY_CMD", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("DECOMPUTE_HOME", str(tmp_path / "nope"))
    assert cli._find_gateway_launcher() is None
