"""add workload_class to usage_events

Revision ID: 0002_workload_class
Revises: 0001_v0_p0
Create Date: 2026-05-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_workload_class"
down_revision = "0001_v0_p0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("workload_class", sa.String(32), nullable=True))
    op.create_index("ix_usage_events_workload_class", "usage_events", ["workload_class"])
    op.create_index(
        "ix_usage_project_workload_created",
        "usage_events",
        ["project_id", "workload_class", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_project_workload_created", table_name="usage_events")
    op.drop_index("ix_usage_events_workload_class", table_name="usage_events")
    op.drop_column("usage_events", "workload_class")
