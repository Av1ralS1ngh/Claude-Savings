"""device licensing: licenses, device_quotas, stripe_events (BYOK/self-hosted axis)

Revision ID: 0006_device_licensing
Revises: 0005_billing
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_device_licensing"
down_revision = "0005_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Uniqueness is enforced via CREATE UNIQUE INDEX (SQLite-compatible; ALTER ADD CONSTRAINT is not).
    op.create_table(
        "licenses",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("license_key_hash", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(32), nullable=False, server_default="pro"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(96), nullable=True),
        sa.Column("bound_device_hash", sa.String(64), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_licenses_key_hash", "licenses", ["license_key_hash"], unique=True)
    op.create_index("ix_licenses_checkout_session", "licenses", ["stripe_checkout_session_id"], unique=True)
    op.create_index("ix_licenses_stripe_customer", "licenses", ["stripe_customer_id"])
    op.create_index("ix_licenses_stripe_subscription", "licenses", ["stripe_subscription_id"])
    op.create_index("ix_licenses_bound_device", "licenses", ["bound_device_hash"])
    op.create_index("ix_licenses_device_status", "licenses", ["bound_device_hash", "status"])

    op.create_table(
        "device_quotas",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("device_hash", sa.String(64), nullable=False),
        sa.Column("compressions_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_quotas_device", "device_quotas", ["device_hash"], unique=True)

    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stripe_events")
    op.drop_index("ix_device_quotas_device", table_name="device_quotas")
    op.drop_table("device_quotas")
    for ix in ("ix_licenses_device_status", "ix_licenses_bound_device", "ix_licenses_stripe_subscription",
               "ix_licenses_stripe_customer", "ix_licenses_checkout_session", "ix_licenses_key_hash"):
        op.drop_index(ix, table_name="licenses")
    op.drop_table("licenses")
