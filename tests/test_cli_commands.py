"""CLI smoke tests for the offline-capable commands.

status / limits / keys hit the gateway and are exercised by the live smoke; here we cover
the two that don't need a running gateway: `version` and `support bundle --dry-run` (the
privacy-preview path — it must produce NO artifact).
"""
from pathlib import Path

from typer.testing import CliRunner

from decompute_ai.cli import app

runner = CliRunner()


def test_version_runs() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "decompute-ai" in result.stdout


def test_support_bundle_dry_run_keeps_nothing(tmp_path: Path) -> None:
    data = tmp_path / ".decompute"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "gateway.log").write_text("ok request_id=req_1\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["support", "bundle", "--all", "--dry-run", "--data-dir", str(data), "--project-root", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    # The whole point: a dry run must not leave a zip (or its leak-scan sidecar) on disk.
    assert not list((data / "support").glob("*.zip"))
    assert not list((data / "support").glob("*.leak_scan.json"))
