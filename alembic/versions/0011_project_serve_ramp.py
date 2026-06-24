"""projects.local_student_serve_enabled: per-project auto-route ramp opt-in

Lets an operator enable inline student serving for specific projects (the canary), and ramp down a
single project without a redeploy. Only ever narrows the serve gate.

Revision ID: 0011_project_serve_ramp
Revises: 0010_ts_calibration
Create Date: 2026-06-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_project_serve_ramp"
down_revision = "0010_ts_calibration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default false: existing projects are NOT in the auto-route canary until explicitly enabled.
    op.add_column("projects", sa.Column("local_student_serve_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("projects", "local_student_serve_enabled")
