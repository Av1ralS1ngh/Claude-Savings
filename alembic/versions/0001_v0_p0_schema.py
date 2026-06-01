"""v0 p0 production schema

Revision ID: 0001_v0_p0
Revises:
Create Date: 2026-05-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_v0_p0"
down_revision = None
branch_labels = None
depends_on = None

json_type = sa.JSON


def upgrade() -> None:
    op.create_table("orgs", sa.Column("id", sa.String(48), primary_key=True), sa.Column("name", sa.String(255), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("users", sa.Column("id", sa.String(48), primary_key=True), sa.Column("email", sa.String(320), nullable=False), sa.Column("display_name", sa.String(255)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table("memberships", sa.Column("id", sa.String(48), primary_key=True), sa.Column("org_id", sa.String(48), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", sa.String(48), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"))
    op.create_index("ix_memberships_org_id", "memberships", ["org_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table("projects", sa.Column("id", sa.String(48), primary_key=True), sa.Column("org_id", sa.String(48), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(255), nullable=False), sa.Column("allowed_models_json", json_type), sa.Column("monthly_spend_cap_usd", sa.Float), sa.Column("ccr_enabled", sa.Boolean, nullable=False, server_default=sa.false()), sa.Column("ccr_retention_days", sa.Integer, nullable=False, server_default="7"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_projects_org_id", "projects", ["org_id"])
    op.create_index("ix_projects_org_created", "projects", ["org_id", "created_at"])
    op.create_table("api_keys", sa.Column("id", sa.String(48), primary_key=True), sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(255), nullable=False), sa.Column("key_hash", sa.String(128), nullable=False), sa.Column("key_prefix", sa.String(24), nullable=False), sa.Column("scopes_json", json_type, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_used_at", sa.DateTime(timezone=True)), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_project_revoked", "api_keys", ["project_id", "revoked_at"])
    op.create_table("devices", sa.Column("id", sa.String(64), primary_key=True), sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("profile_json", json_type, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_devices_project_updated", "devices", ["project_id", "updated_at"])
    op.create_table("deployments", sa.Column("id", sa.String(48), primary_key=True), sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(255), nullable=False), sa.Column("plan_json", json_type, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("project_id", "name", name="uq_deployment_project_name"))
    op.create_index("ix_deployments_project_id", "deployments", ["project_id"])
    op.create_table("ccr_items", sa.Column("id", sa.String(64), primary_key=True), sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("encrypted_payload", sa.LargeBinary), sa.Column("encryption_key_id", sa.String(128)), sa.Column("storage_mode", sa.String(32), nullable=False), sa.Column("redacted_summary", sa.Text), sa.Column("secret_scan_status", sa.String(32), nullable=False), sa.Column("metadata_json", json_type, nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_ccr_items_project_id", "ccr_items", ["project_id"])
    op.create_index("ix_ccr_items_sha256", "ccr_items", ["sha256"])
    op.create_index("ix_ccr_project_created", "ccr_items", ["project_id", "created_at"])
    op.create_index("ix_ccr_items_expires_at", "ccr_items", ["expires_at"])
    op.create_table("usage_events", sa.Column("id", sa.String(48), primary_key=True), sa.Column("project_id", sa.String(48), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("api_key_prefix", sa.String(24)), sa.Column("request_id", sa.String(64)), sa.Column("upstream_request_id", sa.String(128)), sa.Column("endpoint", sa.String(128), nullable=False), sa.Column("model", sa.String(128)), sa.Column("status_code", sa.Integer), sa.Column("latency_ms", sa.Integer), sa.Column("raw_input_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("yscompress_input_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("yellowstone_final_input_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("cache_creation_input_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("cache_read_input_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("tokens_saved", sa.Integer, nullable=False, server_default="0"), sa.Column("billable_input_equivalent_tokens", sa.Integer, nullable=False, server_default="0"), sa.Column("estimated_cost_usd", sa.Float, nullable=False, server_default="0"), sa.Column("estimated_savings_usd", sa.Float, nullable=False, server_default="0"), sa.Column("compression_ratio", sa.Float, nullable=False, server_default="1"), sa.Column("cache_mode", sa.String(32)), sa.Column("route_json", json_type), sa.Column("optimization_steps_json", json_type), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_usage_project_created", "usage_events", ["project_id", "created_at"])
    op.create_index("ix_usage_project_model_created", "usage_events", ["project_id", "model", "created_at"])
    op.create_index("ix_usage_events_request_id", "usage_events", ["request_id"])
    op.create_index("ix_usage_events_upstream_request_id", "usage_events", ["upstream_request_id"])
    op.create_table("audit_logs", sa.Column("id", sa.String(48), primary_key=True), sa.Column("project_id", sa.String(48)), sa.Column("actor_key_prefix", sa.String(24)), sa.Column("action", sa.String(128), nullable=False), sa.Column("target_type", sa.String(64)), sa.Column("target_id", sa.String(128)), sa.Column("request_id", sa.String(64)), sa.Column("ip_address", sa.String(64)), sa.Column("metadata_json", json_type, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_audit_project_created", "audit_logs", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("usage_events")
    op.drop_table("ccr_items")
    op.drop_table("deployments")
    op.drop_table("devices")
    op.drop_table("api_keys")
    op.drop_table("projects")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("orgs")
