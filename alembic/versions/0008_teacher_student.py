"""teacher-student: capture-consent project columns + runs/cases/verdicts tables

Revision ID: 0008_teacher_student
Revises: 0007_usage_client
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_teacher_student"
down_revision = "0007_usage_client"
branch_labels = None
depends_on = None

# JSON on SQLite (tests/dev), JSONB on Postgres (prod) — matches the ORM's JSONField (L1).
_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    # ── Project-level capture consent (defaults keep every existing project metadata-only) ──
    op.add_column("projects", sa.Column("teacher_student_capture_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("projects", sa.Column("teacher_student_governance_mode", sa.String(32), nullable=False, server_default="metadata_only"))
    op.add_column("projects", sa.Column("teacher_student_capture_retention_days", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("projects", sa.Column("teacher_student_capture_locked", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "teacher_student_runs",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("mode", sa.String(32), nullable=False, server_default="student_first"),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("student_model", sa.String(128), nullable=True),
        sa.Column("teacher_model", sa.String(128), nullable=True),
        sa.Column("governance_mode", sa.String(32), nullable=False, server_default="metadata_only"),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("teacher_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_teacher_student_runs_project_id", "teacher_student_runs", ["project_id"])
    op.create_index("ix_ts_runs_project_created", "teacher_student_runs", ["project_id", "created_at"])

    op.create_table(
        "teacher_student_cases",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("run_id", sa.String(48), sa.ForeignKey("teacher_student_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usage_event_id", sa.String(48), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("client", sa.String(32), nullable=True),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column("workload_class", sa.String(32), nullable=True),
        sa.Column("coding_intent", sa.String(32), nullable=True),
        sa.Column("escalation_reason", sa.String(64), nullable=True),
        sa.Column("storage_mode", sa.String(32), nullable=False, server_default="metadata_only"),
        sa.Column("prompt_sha256", sa.String(64), nullable=True),
        sa.Column("feature_json", _JSON, nullable=True),
        sa.Column("student_answer", sa.Text(), nullable=True),
        sa.Column("teacher_answer", sa.Text(), nullable=True),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("tests_json", _JSON, nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("student_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("teacher_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("student_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("teacher_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_teacher_student_cases_run_id", "teacher_student_cases", ["run_id"])
    op.create_index("ix_teacher_student_cases_project_id", "teacher_student_cases", ["project_id"])
    op.create_index("ix_teacher_student_cases_usage_event_id", "teacher_student_cases", ["usage_event_id"])
    op.create_index("ix_teacher_student_cases_request_id", "teacher_student_cases", ["request_id"])
    op.create_index("ix_teacher_student_cases_session_id", "teacher_student_cases", ["session_id"])
    op.create_index("ix_teacher_student_cases_client", "teacher_student_cases", ["client"])
    op.create_index("ix_teacher_student_cases_expires_at", "teacher_student_cases", ["expires_at"])
    op.create_index("ix_ts_cases_project_created", "teacher_student_cases", ["project_id", "created_at"])
    op.create_index("ix_ts_cases_run_created", "teacher_student_cases", ["run_id", "created_at"])

    op.create_table(
        "teacher_student_verdicts",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("case_id", sa.String(48), sa.ForeignKey("teacher_student_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verifier_version", sa.String(32), nullable=True),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0"),
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("escalation_reason", sa.String(64), nullable=True),
        sa.Column("tests_ran", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tests_passed", sa.Boolean(), nullable=True),
        sa.Column("sandbox_backend", sa.String(32), nullable=True),
        sa.Column("judge_model", sa.String(128), nullable=True),
        sa.Column("layers_json", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_teacher_student_verdicts_case_id", "teacher_student_verdicts", ["case_id"])
    op.create_index("ix_ts_verdicts_case", "teacher_student_verdicts", ["case_id"])


def downgrade() -> None:
    op.drop_table("teacher_student_verdicts")
    op.drop_table("teacher_student_cases")
    op.drop_table("teacher_student_runs")
    op.drop_column("projects", "teacher_student_capture_locked")
    op.drop_column("projects", "teacher_student_capture_retention_days")
    op.drop_column("projects", "teacher_student_governance_mode")
    op.drop_column("projects", "teacher_student_capture_enabled")
