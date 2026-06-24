"""usage_events: first-class observe-mode columns for teacher-student auto-route decisioning

Promotes the teacher-student observe-mode signals from route_json into indexed columns so the
"would the local student have served, and did it?" question can be aggregated cheaply (the
observe -> auto-route ramp decision data).

Revision ID: 0009_observe_mode_columns
Revises: 0008_teacher_student
Create Date: 2026-06-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_observe_mode_columns"
down_revision = "0008_teacher_student"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable/defaulted, so existing rows need no backfill (they read as "not a candidate").
    op.add_column("usage_events", sa.Column("local_student_candidate", sa.Boolean(), nullable=True, server_default=sa.false()))
    op.add_column("usage_events", sa.Column("coding_intent", sa.String(32), nullable=True))
    op.add_column("usage_events", sa.Column("served_by_student", sa.Boolean(), nullable=True, server_default=sa.false()))
    op.create_index("ix_usage_events_ls_candidate", "usage_events", ["local_student_candidate"])
    op.create_index("ix_usage_events_coding_intent", "usage_events", ["coding_intent"])
    op.create_index("ix_usage_events_served_by_student", "usage_events", ["served_by_student"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_served_by_student", table_name="usage_events")
    op.drop_index("ix_usage_events_coding_intent", table_name="usage_events")
    op.drop_index("ix_usage_events_ls_candidate", table_name="usage_events")
    op.drop_column("usage_events", "served_by_student")
    op.drop_column("usage_events", "coding_intent")
    op.drop_column("usage_events", "local_student_candidate")
