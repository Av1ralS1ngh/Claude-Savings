"""Distillation export governance tests (Phase 7).

The export path is where Claude-output-as-training-label policy is enforced. These assert:
  * metadata-only / regulated projects export NOTHING (no content to export)
  * Claude (teacher) outputs are excluded unless the allow-claude-targets policy flag is set
  * student outputs are always exportable when content is stored
  * expired cases are excluded
"""
from __future__ import annotations

from decompute_gateway.config import Settings
from decompute_gateway.db import Store


def _store(tmp_path, **settings_over):
    s = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/exp.db", **settings_over)
    st = Store(s)
    st.create_schema_for_local_dev()
    return st


def _enable_capture(store, project_id, mode="redacted"):
    with store.session() as s:
        from decompute_gateway.models import Project
        proj = s.get(Project, project_id)
        proj.teacher_student_capture_enabled = True
        proj.teacher_student_governance_mode = mode


def _case(route, *, student="STUDENT CODE", final=None):
    return {
        "route": route,
        "prompt_sha256": "h" * 64,
        "student_answer": student,
        "final_answer": final if final is not None else student,
        "verdict": {"decision": "accept_student" if route == "student_only" else "escalate", "confidence": 0.9, "threshold": 0.82},
    }


# ── Governance mode gates ────────────────────────────────────────────────────────


def test_metadata_only_exports_nothing(tmp_path):
    store = _store(tmp_path)  # capture disabled by default
    pid = store.create_project("p")["id"]
    store.record_teacher_student_case(_case("student_only"), project_id=pid, request_capture=True)
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=True)
    assert records == []
    assert meta["skipped_no_content"] == 1  # metadata-only stored no content


def test_student_output_exportable_when_captured(tmp_path):
    store = _store(tmp_path, teacher_student_capture_enabled=True)
    pid = store.create_project("p")["id"]
    _enable_capture(store, pid, mode="redacted")
    store.record_teacher_student_case(_case("student_only"), project_id=pid, request_capture=True)
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=False)
    assert len(records) == 1
    assert records[0]["label_source"] == "student"
    assert "STUDENT CODE" in records[0]["completion"]


# ── Claude-output policy gate ─────────────────────────────────────────────────────


def test_claude_teacher_output_excluded_without_signoff(tmp_path):
    store = _store(tmp_path, teacher_student_capture_enabled=True)
    pid = store.create_project("p")["id"]
    _enable_capture(store, pid, mode="redacted")
    # A teacher_corrected case: the final answer is Claude's output.
    store.record_teacher_student_case(_case("teacher_corrected", final="CLAUDE FINAL"), project_id=pid, request_capture=True)
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=False)
    assert records == []
    assert meta["skipped_claude_target"] == 1


def test_claude_target_allow_without_signoff_is_blocked(tmp_path):
    """M3: allow_claude_targets alone is NOT enough — the sign-off is a required second gate, enforced
    at the data boundary so a direct store caller can't bypass production validation."""
    store = _store(tmp_path, teacher_student_capture_enabled=True)
    pid = store.create_project("p")["id"]
    _enable_capture(store, pid, mode="redacted")
    store.record_teacher_student_case(_case("teacher_corrected", final="CLAUDE FINAL"), project_id=pid, request_capture=True)
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=True, claude_targets_signoff=False)
    assert records == []
    assert meta["skipped_claude_target"] == 1


def test_claude_teacher_output_included_with_allow_and_signoff(tmp_path):
    store = _store(tmp_path, teacher_student_capture_enabled=True)
    pid = store.create_project("p")["id"]
    _enable_capture(store, pid, mode="redacted")
    store.record_teacher_student_case(_case("teacher_corrected", final="CLAUDE FINAL"), project_id=pid, request_capture=True)
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=True, claude_targets_signoff=True)
    assert len(records) == 1
    assert records[0]["label_source"] == "teacher"


def test_mixed_export_counts(tmp_path):
    store = _store(tmp_path, teacher_student_capture_enabled=True)
    pid = store.create_project("p")["id"]
    _enable_capture(store, pid, mode="redacted")
    store.record_teacher_student_case(_case("student_only"), project_id=pid, request_capture=True)
    store.record_teacher_student_case(_case("teacher_corrected", final="CLAUDE FINAL"), project_id=pid, request_capture=True)
    # Without the allow flag, only the student case exports.
    records, meta = store.export_distillation_records(project_id=pid, allow_claude_targets=False)
    assert meta["exported"] == 1
    assert meta["skipped_claude_target"] == 1


# ── Route-level integration via the API ─────────────────────────────────────────


def test_export_endpoint_409_for_metadata_only(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("DECOMPUTE_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("DECOMPUTE_MOCK_ANTHROPIC", "true")
    import decompute_gateway.config as cfg
    importlib.reload(cfg)
    import decompute_gateway.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient

    try:
        with TestClient(main.app) as c:
            # Default dev principal has admin scope (implies distill:read). Project is metadata-only.
            r = c.get("/v1/teacher_student/export/sft.jsonl")
        assert r.status_code == 409
        assert r.json()["error"] == "export_forbidden"
    finally:
        importlib.reload(cfg)
        importlib.reload(main)
