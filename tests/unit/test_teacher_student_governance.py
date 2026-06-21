"""Store-boundary governance tests (Phase 3).

The store is the privacy boundary. These assert:
  * default metadata-only mode persists NO prompt/answer/test content
  * enum-like values are clamped to closed sets
  * content is stored only when capture is explicitly enabled AND mode permits it
  * capture_locked / server-disabled both force metadata-only
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from decompute_gateway.config import Settings
from decompute_gateway.db import Store


@pytest.fixture
def store(tmp_path):
    s = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/gov.db")
    st = Store(s)
    st.create_schema_for_local_dev()
    return st


def _project(store) -> str:
    return store.create_project("ts-gov")["id"]


def _case_payload(**over):
    base = dict(
        request_id="dreq_1",
        route="student_only",
        escalation_reason=None,
        prompt_sha256="a" * 64,
        student_answer="SECRET STUDENT CODE",
        teacher_answer="SECRET TEACHER CODE",
        final_answer="SECRET FINAL CODE",
        tests_json={"tests": "assert add(2,3)==5"},
        input_tokens=10,
        student_output_tokens=5,
        verdict={"decision": "accept_student", "confidence": 0.9, "threshold": 0.82, "escalated": False},
    )
    base.update(over)
    return base


# ── Metadata-only default stores no content ────────────────────────────────────


def test_metadata_only_default_stores_no_content(store):
    pid = _project(store)
    rec = store.record_teacher_student_case(_case_payload(), project_id=pid, request_capture=True)
    assert rec["storage_mode"] == "metadata_only"
    # Read the raw row back: content columns must be NULL, metadata present.
    with store.session() as s:
        from decompute_gateway.models import TeacherStudentCase
        row = s.query(TeacherStudentCase).filter_by(id=rec["id"]).one()
        assert row.student_answer is None
        assert row.teacher_answer is None
        assert row.final_answer is None
        assert row.tests_json is None
        # ...but the privacy-safe correlators ARE kept — and the prompt hash is HMAC-SALTED (M4),
        # so it is NOT the raw digest the caller passed in.
        assert row.prompt_sha256 is not None and row.prompt_sha256 != "a" * 64
        assert len(row.prompt_sha256) == 64  # still a sha256 hex digest
        assert row.input_tokens == 10
        assert row.expires_at is None  # no retention clock when nothing is stored


def test_server_capture_off_forces_metadata_only_even_if_project_opts_in(store):
    pid = _project(store)
    # Project opts in, but the server-level flag (default off) wins -> metadata_only.
    with store.session() as s:
        from decompute_gateway.models import Project
        proj = s.get(Project, pid)
        proj.teacher_student_capture_enabled = True
        proj.teacher_student_governance_mode = "redacted"
    assert store.effective_capture_mode(pid) == "metadata_only"


# ── Capture enabled at both levels -> content stored, with retention ────────────


def test_redacted_mode_stores_content_with_expiry(tmp_path):
    s = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/cap.db",
        teacher_student_capture_enabled=True,
        teacher_student_capture_retention_days=7,
    )
    store = Store(s)
    store.create_schema_for_local_dev()
    pid = store.create_project("cap")["id"]
    with store.session() as sess:
        from decompute_gateway.models import Project
        proj = sess.get(Project, pid)
        proj.teacher_student_capture_enabled = True
        proj.teacher_student_governance_mode = "redacted"
    assert store.effective_capture_mode(pid) == "redacted"
    rec = store.record_teacher_student_case(_case_payload(), project_id=pid, request_capture=True)
    with store.session() as sess:
        from decompute_gateway.models import TeacherStudentCase
        row = sess.query(TeacherStudentCase).filter_by(id=rec["id"]).one()
        assert row.student_answer == "SECRET STUDENT CODE"
        assert row.expires_at is not None  # retention clock set


def test_capture_locked_forces_metadata_only(tmp_path):
    s = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/lock.db",
        teacher_student_capture_enabled=True,
    )
    store = Store(s)
    store.create_schema_for_local_dev()
    pid = store.create_project("lock")["id"]
    with store.session() as sess:
        from decompute_gateway.models import Project
        proj = sess.get(Project, pid)
        proj.teacher_student_capture_enabled = True
        proj.teacher_student_governance_mode = "redacted"
        proj.teacher_student_capture_locked = True  # overrides opt-in
    assert store.effective_capture_mode(pid) == "metadata_only"
    rec = store.record_teacher_student_case(_case_payload(), project_id=pid, request_capture=True)
    with store.session() as sess:
        from decompute_gateway.models import TeacherStudentCase
        row = sess.query(TeacherStudentCase).filter_by(id=rec["id"]).one()
        assert row.student_answer is None  # locked -> no content despite opt-in


def test_regulated_mode_stores_no_content(tmp_path):
    s = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/reg.db",
        teacher_student_capture_enabled=True,
    )
    store = Store(s)
    store.create_schema_for_local_dev()
    pid = store.create_project("reg")["id"]
    with store.session() as sess:
        from decompute_gateway.models import Project
        proj = sess.get(Project, pid)
        proj.teacher_student_capture_enabled = True
        proj.teacher_student_governance_mode = "regulated"
    assert store.effective_capture_mode(pid) == "regulated"
    rec = store.record_teacher_student_case(_case_payload(), project_id=pid, request_capture=True)
    with store.session() as sess:
        from decompute_gateway.models import TeacherStudentCase
        row = sess.query(TeacherStudentCase).filter_by(id=rec["id"]).one()
        assert row.student_answer is None  # regulated = hashes/features only


# ── Enum clamping at the boundary ──────────────────────────────────────────────


def test_bad_route_is_clamped(store):
    pid = _project(store)
    rec = store.record_teacher_student_case(_case_payload(route="HACKED'; DROP TABLE--"), project_id=pid, request_capture=True)
    assert rec["route"] == "student_unavailable"  # clamped to a safe default


def test_bad_decision_is_clamped(store):
    pid = _project(store)
    payload = _case_payload(verdict={"decision": "totally_made_up", "confidence": 0.5, "threshold": 0.8})
    rec = store.record_teacher_student_case(payload, project_id=pid)
    assert rec["verdict"]["decision"] == "escalate"  # clamped


def test_bad_client_becomes_null(store):
    pid = _project(store)
    rec = store.record_teacher_student_case(_case_payload(client="raw-user-agent/1.0"), project_id=pid, request_capture=True)
    with store.session() as s:
        from decompute_gateway.models import TeacherStudentCase
        row = s.query(TeacherStudentCase).filter_by(id=rec["id"]).one()
        assert row.client is None


# ── Run lifecycle + summary ────────────────────────────────────────────────────


def test_run_case_count_increments(store):
    pid = _project(store)
    run = store.create_teacher_student_run(project_id=pid, name="batch")
    store.record_teacher_student_case(_case_payload(route="student_only"), project_id=pid, run_id=run["id"], request_capture=True)
    store.record_teacher_student_case(_case_payload(route="teacher_corrected"), project_id=pid, run_id=run["id"], request_capture=True)
    runs = store.list_teacher_student_runs(project_id=pid)
    assert runs[0]["case_count"] == 2
    assert runs[0]["teacher_call_count"] == 1


def test_summary_counts_only(store):
    pid = _project(store)
    store.record_teacher_student_case(_case_payload(route="student_only"), project_id=pid, request_capture=True)
    store.record_teacher_student_case(_case_payload(route="teacher_corrected"), project_id=pid, request_capture=True)
    summary = store.teacher_student_summary(project_id=pid)
    assert summary["cases"] == 2
    assert summary["student_accepted"] == 1
    assert summary["teacher_called"] == 1
    assert summary["escalation_rate"] == 0.5


# ── Schema sanity ──────────────────────────────────────────────────────────────


def test_new_tables_and_columns_exist(store):
    insp = inspect(store.engine)
    tables = set(insp.get_table_names())
    assert {"teacher_student_runs", "teacher_student_cases", "teacher_student_verdicts"} <= tables
    proj_cols = {c["name"] for c in insp.get_columns("projects")}
    assert {"teacher_student_capture_enabled", "teacher_student_capture_locked"} <= proj_cols
