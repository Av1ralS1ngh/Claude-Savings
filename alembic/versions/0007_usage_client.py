"""usage_events.client: which harness drove the request (claude_code/openclaw/sdk/other)

Revision ID: 0007_usage_client
Revises: 0006_device_licensing
Create Date: 2026-06-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_usage_client"
down_revision = "0006_device_licensing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so existing rows need no backfill (they read as "unknown client").
    op.add_column("usage_events", sa.Column("client", sa.String(32), nullable=True))
    op.create_index("ix_usage_events_client", "usage_events", ["client"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_client", table_name="usage_events")
    op.drop_column("usage_events", "client")
