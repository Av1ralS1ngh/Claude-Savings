"""teacher_student_calibration: shadow-eval false-accept signal for auto-route readiness

A metadata-only table recording out-of-band shadow evaluations: would the verifier have accepted the
student answer, and did it agree with Claude's actual answer? (would_accept & !agreed = false accept).

Revision ID: 0010_ts_calibration
Revises: 0009_observe_mode_columns
Create Date: 2026-06-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_ts_calibration"
down_revision = "0009_observe_mode_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teacher_student_calibration",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("coding_intent", sa.String(32)),
        sa.Column("prompt_sha256", sa.String(64)),
        sa.Column("would_accept", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("agreed_with_teacher", sa.Boolean()),
        sa.Column("judge_model", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ts_calib_project", "teacher_student_calibration", ["project_id"])
    op.create_index("ix_ts_calib_request", "teacher_student_calibration", ["request_id"])
    op.create_index("ix_ts_calib_intent", "teacher_student_calibration", ["coding_intent"])
    op.create_index("ix_ts_calib_would_accept", "teacher_student_calibration", ["would_accept"])
    op.create_index("ix_ts_calib_agreed", "teacher_student_calibration", ["agreed_with_teacher"])
    op.create_index("ix_ts_calib_project_created", "teacher_student_calibration", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_table("teacher_student_calibration")
