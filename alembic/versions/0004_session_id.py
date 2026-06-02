"""add session_id to usage_events for session-scoped support reports

Revision ID: 0004_session_id
Revises: 0003_stream_accounting
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_session_id"
down_revision = "0003_stream_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("session_id", sa.String(128), nullable=True))
    op.create_index("ix_usage_events_session_id", "usage_events", ["session_id"])
    op.create_index("ix_usage_session_created", "usage_events", ["project_id", "session_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_session_created", table_name="usage_events")
    op.drop_index("ix_usage_events_session_id", table_name="usage_events")
    op.drop_column("usage_events", "session_id")
