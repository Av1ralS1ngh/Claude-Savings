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


# Closed set of harness buckets allowed in UsageEvent.client. Enforced at the storage
# boundary (db.record_usage) so a raw User-Agent can never be persisted, even if a future
# caller bypasses detect_client(). Anything outside this set is normalized to NULL.
CLIENT_BUCKETS = frozenset({"claude_code", "openclaw", "sdk", "other"})

# Closed enum sets for the teacher-student tables, enforced at the store boundary (db.py) exactly
# like CLIENT_BUCKETS. A value outside the set is normalized to a safe default, never persisted raw.
TS_MODES = frozenset({"student_first", "teacher_first", "parallel", "distill"})
TS_ROUTES = frozenset({"student_only", "teacher_corrected", "teacher_only", "student_unavailable", "disabled"})
TS_STORAGE_MODES = frozenset({"metadata_only", "redacted", "regulated"})
TS_GOVERNANCE_MODES = frozenset({"metadata_only", "redacted", "regulated"})
TS_DECISIONS = frozenset({"accept_student", "escalate"})


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
    # Teacher-student capture consent (plan §"Minimal V1 Tables"). BOTH server config and project
    # config must allow capture; capture_locked=true forces metadata-only regardless of opt-in.
    teacher_student_capture_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    teacher_student_governance_mode: Mapped[str] = mapped_column(String(32), default="metadata_only", nullable=False)
    teacher_student_capture_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    teacher_student_capture_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    # Which harness drove the request, derived from User-Agent: 'claude_code',
    # 'openclaw', 'sdk', 'other', or NULL (unknown). Lets the dashboard split savings
    # by client without storing the raw UA. See detect_client() in main.py.
    client: Mapped[str | None] = mapped_column(String(32), index=True)
    cache_mode: Mapped[str | None] = mapped_column(String(32))
    route_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    # Teacher-student observe-mode signals on the /v1/messages path, promoted from route_json to
    # indexed columns so the "would the student have served?" question can be aggregated cheaply
    # (the observe -> auto-route decision data). local_student_candidate: the request was eligible for
    # the local student. coding_intent: the classified intent (simple_code_patch/.../unknown).
    # served_by_student: True only when the student actually served this request (opt-in serve path).
    local_student_candidate: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
    coding_intent: Mapped[str | None] = mapped_column(String(32), index=True)
    served_by_student: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
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


class License(Base):
    """A device-bound Pro license backed by a Stripe subscription (the BYOK/self-hosted axis).

    Issued ONCE per paid Checkout Session (stripe_checkout_session_id is UNIQUE), the raw key is
    shown to the user once and only its HMAC is stored. The license is bound to a single device
    (bound_device_hash) and stays Pro while the subscription is active (current_period_end +
    grace). Revoked on subscription deletion. This is the ONLY authority for device Pro access.
    """
    __tablename__ = "licenses"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)  # lic_*
    license_key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    tier: Mapped[str] = mapped_column(String(32), default="pro", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active|revoked
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # UNIQUE -> exactly one license per paid checkout session (single-redeem guarantee).
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(96), unique=True, index=True)
    # The device this license currently entitles; rebindable (device migration). NULL until activated.
    bound_device_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_licenses_device_status", "bound_device_hash", "status"),)


class DeviceQuota(Base):
    """Per-device FREE-tier compression counter (the free unit). Keyed by the server-derived
    device-hash (HMAC of the client fingerprint), reset per window (daily). The client never
    supplies or echoes the count — it lives here authoritatively, so it cannot be rolled back."""
    __tablename__ = "device_quotas"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)  # devq_*
    device_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    compressions_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_date: Mapped[str] = mapped_column(String(10), default="", nullable=False)  # 'YYYY-MM-DD' (daily) or 'lifetime'
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StripeEvent(Base):
    """Webhook idempotency: a redelivered event (same Stripe event id) is processed once."""
    __tablename__ = "stripe_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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


# ── Teacher-student tables (plan §"Data Model And Migrations") ─────────────────
# Default storage is metadata-only: content columns (prompts, answers, tests) stay NULL unless
# capture is explicitly enabled at BOTH server and project level. The store boundary (db.py) is what
# enforces this — these columns merely have room for content when governance permits it.


class TeacherStudentRun(Base):
    __tablename__ = "teacher_student_runs"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(32), default="student_first", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    student_model: Mapped[str | None] = mapped_column(String(128))
    teacher_model: Mapped[str | None] = mapped_column(String(128))
    governance_mode: Mapped[str] = mapped_column(String(32), default="metadata_only", nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    teacher_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (Index("ix_ts_runs_project_created", "project_id", "created_at"),)


class TeacherStudentCase(Base):
    __tablename__ = "teacher_student_cases"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("teacher_student_runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    usage_event_id: Mapped[str | None] = mapped_column(String(48), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    client: Mapped[str | None] = mapped_column(String(32), index=True)
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    workload_class: Mapped[str | None] = mapped_column(String(32))
    coding_intent: Mapped[str | None] = mapped_column(String(32))
    escalation_reason: Mapped[str | None] = mapped_column(String(64))
    storage_mode: Mapped[str] = mapped_column(String(32), default="metadata_only", nullable=False)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    feature_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    # Content columns — NULL in metadata-only mode (the default).
    student_answer: Mapped[str | None] = mapped_column(Text)
    teacher_answer: Mapped[str | None] = mapped_column(Text)
    final_answer: Mapped[str | None] = mapped_column(Text)
    tests_json: Mapped[dict[str, Any] | None] = mapped_column(JSONField)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    student_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    teacher_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    student_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    teacher_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_ts_cases_project_created", "project_id", "created_at"),
        Index("ix_ts_cases_run_created", "run_id", "created_at"),
    )


class TeacherStudentVerdict(Base):
    __tablename__ = "teacher_student_verdicts"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("teacher_student_cases.id", ondelete="CASCADE"), index=True)
    verifier_version: Mapped[str | None] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(64))
    tests_ran: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tests_passed: Mapped[bool | None] = mapped_column(Boolean)
    sandbox_backend: Mapped[str | None] = mapped_column(String(32))
    judge_model: Mapped[str | None] = mapped_column(String(128))
    layers_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONField)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_ts_verdicts_case", "case_id"),)
