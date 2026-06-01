"""add streaming accounting columns to usage_events

Revision ID: 0003_stream_accounting
Revises: 0002_workload_class
Create Date: 2026-06-02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_stream_accounting"
down_revision = "0002_workload_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("stream_status", sa.String(40), nullable=True))
    op.add_column(
        "usage_events",
        sa.Column("stream_cancelled", sa.Boolean(), nullable=True, server_default=sa.text("false")),
    )
    op.add_column("usage_events", sa.Column("stream_cancel_reason", sa.String(64), nullable=True))
    # JSON on SQLite (tests), JSONB on Postgres (prod) — matches the ORM's JSONField.
    json_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.add_column("usage_events", sa.Column("stream_json", json_type, nullable=True))
    op.create_index("ix_usage_events_stream_status", "usage_events", ["stream_status"])
    op.create_index(
        "ix_usage_project_stream_status_created",
        "usage_events",
        ["project_id", "stream_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_project_stream_status_created", table_name="usage_events")
    op.drop_index("ix_usage_events_stream_status", table_name="usage_events")
    op.drop_column("usage_events", "stream_json")
    op.drop_column("usage_events", "stream_cancel_reason")
    op.drop_column("usage_events", "stream_cancelled")
    op.drop_column("usage_events", "stream_status")
