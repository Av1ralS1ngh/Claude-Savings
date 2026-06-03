"""add billing/licensing columns to projects (tier + Stripe subscription)

Revision ID: 0005_billing
Revises: 0004_session_id
Create Date: 2026-06-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_billing"
down_revision = "0004_session_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("tier", sa.String(32), nullable=False, server_default="free"))
    op.add_column("projects", sa.Column("stripe_customer_id", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("stripe_subscription_id", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("subscription_status", sa.String(32), nullable=True))
    op.add_column("projects", sa.Column("subscription_current_period_end", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_projects_stripe_customer", "projects", ["stripe_customer_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_stripe_customer", table_name="projects")
    op.drop_column("projects", "subscription_current_period_end")
    op.drop_column("projects", "subscription_status")
    op.drop_column("projects", "stripe_subscription_id")
    op.drop_column("projects", "stripe_customer_id")
    op.drop_column("projects", "tier")
