from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


JSONField = JSON().with_variant(JSONB, "postgresql")


class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_models_json: Mapped[list[str] | None] = mapped_column(JSONField)
    monthly_spend_cap_usd: Mapped[float | None] = mapped_column(Float)
    ccr_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ccr_retention_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    # Billing / licensing: the active Stripe subscription IS the license; tier drives quotas.
    tier: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32))  # active|trialing|past_due|canceled|...
    subscription_current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_projects_org_created", "org_id", "created_at"),
        Index("ix_projects_stripe_customer", "stripe_customer_id"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    scopes_json: Mapped[list[str]] = mapped_column(JSONField, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_api_keys_project_revoked", "project_id", "revoked_at"),)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONField, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_devices_project_updated", "project_id", "updated_at"),)


class Deployment(Base):
    __tablename__ = "deployments"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONField, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_deployment_project_name"),)


class CCRItem(Base):
    __tablename__ = "ccr_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_id: Mapped[str | None] = mapped_column(String(128))
    storage_mode: Mapped[str] = mapped_column(String(32), default="encrypted", nullable=False)  # encrypted|redacted|metadata_only
    redacted_summary: Mapped[str | None] = mapped_column(Text)
    secret_scan_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONField, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_ccr_project_created", "project_id", "created_at"),)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    api_key_prefix: Mapped[str | None] = mapped_column(String(24), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # Client-supplied session correlator (Claude Code's x-decompute-session-id). Groups a
    # run's requests so a privacy-safe support report can be scoped to one session.
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), index=True)
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    yscompress_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    yellowstone_final_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_saved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billable_input_equivalent_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_savings_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    compression_ratio: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    workload_class: Mapped[str | None] = mapped_column(String(32), index=True)
    cache_mode: Mapped[str | None] = mapped_column(String(32))
    route_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    optimization_steps_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONField)
    # Streaming accounting (nullable: only set on streamed requests; legacy rows stay NULL).
    stream_status: Mapped[str | None] = mapped_column(String(40), index=True)
    stream_cancelled: Mapped[bool | None] = mapped_column(Boolean, default=False)
    stream_cancel_reason: Mapped[str | None] = mapped_column(String(64))
    stream_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_usage_project_created", "project_id", "created_at"),
        Index("ix_usage_project_model_created", "project_id", "model", "created_at"),
        Index("ix_usage_project_workload_created", "project_id", "workload_class", "created_at"),
        Index("ix_usage_project_stream_status_created", "project_id", "stream_status", "created_at"),
        Index("ix_usage_session_created", "project_id", "session_id", "created_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(48), index=True)
    actor_key_prefix: Mapped[str | None] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONField, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_audit_project_created", "project_id", "created_at"),)
