from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import secrets
from typing import Any, Iterator

from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .crypto import CCRCrypto
from .models import (
    CLIENT_BUCKETS,
    TS_DECISIONS,
    TS_GOVERNANCE_MODES,
    TS_MODES,
    TS_ROUTES,
    ApiKey,
    AuditLog,
    Base,
    CCRItem,
    Deployment,
    Device,
    DeviceQuota,
    License,
    Membership,
    Org,
    Project,
    StripeEvent,
    TeacherStudentCase,
    TeacherStudentRun,
    TeacherStudentVerdict,
    UsageEvent,
    User,
    utcnow,
)
from .secret_scanner import SecretScanner

log = logging.getLogger("decompute.gateway.db")

# Headline savings median is a ROLLING median over the most recent N requests (newest first),
# so it tracks current performance rather than being diluted by the whole history.
ROLLING_MEDIAN_WINDOW = 50


ALL_SCOPES = [
    "messages:write",
    "usage:read",
    "keys:admin",
    "ccr:read",
    "ccr:delete",
    "deployments:write",
    "deployments:read",
    "devices:read",
    "devices:write",
    "routes:read",
    "dashboard:read",
    "support:read",
    "support:write",
    # Teacher-student / distillation. `messages:write` permits CREATING a run; it does NOT imply
    # listing stored cases or exporting records — those need these dedicated scopes (plan §Privacy).
    "teacher_student:read",
    "teacher_student:admin",
    "distill:read",
    "distill:admin",
    "admin",
]


def utc_ts() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            # Memory SQLite needs StaticPool; file SQLite works with default pool.
            if ":memory:" in settings.database_url:
                engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs["pool_size"] = settings.db_pool_size
            engine_kwargs["max_overflow"] = settings.db_max_overflow
        self.engine: Engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def create_schema_for_local_dev(self) -> None:
        # Enable WAL before touching the DB — prevents "database is locked" under
        # concurrent requests when multiple async tasks hit SQLite at startup.
        if self.settings.uses_sqlite and ":memory:" not in self.settings.database_url:
            with self.engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.commit()
        Base.metadata.create_all(self.engine)
        # create_all only CREATEs missing tables — it never ALTERs an existing one.
        # A SQLite dev DB from an earlier build is missing columns added by later
        # migrations (create_all-vs-alembic drift), and every insert that sets the
        # new column then crashes. Evolve existing tables to match the ORM here so
        # the SQLite dev path survives schema additions the same way alembic does.
        self._evolve_sqlite_columns()
        # Stamp the Alembic version so `alembic upgrade head` run later (e.g. when
        # switching to Postgres) sees the schema as up-to-date. create_all builds the
        # LATEST ORM schema, so we stamp the latest head — never a hard-coded revision.
        head = self._alembic_head()
        if head is not None:  # None when no migrations ship (pip-installed wheel) — skip stamping
            with self.engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num))"
                ))
                existing = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
                if not existing:
                    conn.execute(text("INSERT INTO alembic_version(version_num) VALUES (:v)"), {"v": head})
                elif existing[0] != head:
                    # Schema was just evolved to head by create_all + column shim; advance
                    # the stamp so it matches reality and a later upgrade is a clean no-op.
                    conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": head})
        self.ensure_default_project()

    def _evolve_sqlite_columns(self) -> None:
        """Add ORM columns missing from existing tables (SQLite dev path only).
        SQLite supports ALTER TABLE ADD COLUMN; this closes the create_all drift gap
        for additive migrations without depending on the alembic runner."""
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(self.engine)
        existing_tables = set(insp.get_table_names())
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue  # freshly created by create_all — already complete
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    coltype = col.type.compile(dialect=self.engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}'))

    @staticmethod
    def _alembic_head() -> str | None:
        """Current Alembic head revision, so the SQLite create_all path stamps the same version
        `alembic upgrade head` would. Returns None when the alembic migration scripts aren't present
        — a pip-installed wheel ships no `alembic/` directory, so there's no migration history to
        stamp against and the gateway must still start. Resolves to an ABSOLUTE script path so head
        detection works regardless of process CWD (gunicorn/systemd often run from /)."""
        from pathlib import Path
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
        except ImportError:
            return None
        ini = Path(__file__).resolve().parents[3] / "alembic.ini"
        script_dir = ini.parent / "alembic"
        if not script_dir.exists():
            return None  # installed layout: no migrations shipped — nothing to stamp
        cfg = Config(str(ini)) if ini.exists() else Config()
        cfg.set_main_option("script_location", str(script_dir))
        try:
            return ScriptDirectory.from_config(cfg).get_current_head()
        except Exception:
            return None

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(24).replace('-', '').replace('_', '')[:24]}"

    def hash_key(self, raw_key: str) -> str:
        return hmac.new(self.settings.api_key_hash_salt.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

    def ensure_default_project(self) -> dict[str, Any]:
        try:
            with self.session() as s:
                if not s.get(Org, "org_default"):
                    s.add(Org(id="org_default", name="Default Org"))
                if not s.get(Project, "proj_default"):
                    s.add(Project(
                        id="proj_default",
                        org_id="org_default",
                        name="Default Project",
                        allowed_models_json=None,
                        monthly_spend_cap_usd=self.settings.monthly_spend_usd,
                        ccr_enabled=self.settings.ccr_enabled,
                        ccr_retention_days=self.settings.ccr_retention_days,
                    ))
        except IntegrityError:
            # Concurrent worker already inserted it (multi-worker gunicorn boot).
            # The row now exists — that's the only outcome we care about.
            pass
        return {"id": "proj_default", "org_id": "org_default", "name": "Default Project"}

    def bootstrap_admin(self, email: str = "admin@local", project_id: str = "proj_default") -> dict[str, Any]:
        raw = "dc_" + secrets.token_urlsafe(32)
        now = utc_ts()
        with self.session() as s:
            org = s.get(Org, "org_default") or Org(id="org_default", name="Default Org")
            s.merge(org)
            user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if not user:
                user = User(id=self._id("usr"), email=email, display_name="Bootstrap Admin")
                s.add(user)
            project = s.get(Project, project_id)
            if not project:
                project = Project(id=project_id, org_id=org.id, name="Default Project", monthly_spend_cap_usd=self.settings.monthly_spend_usd)
                s.add(project)
            member = s.execute(select(Membership).where(Membership.org_id == org.id, Membership.user_id == user.id)).scalar_one_or_none()
            if not member:
                s.add(Membership(id=self._id("mem"), org_id=org.id, user_id=user.id, role="owner"))
            key = ApiKey(
                id=self._id("key"),
                project_id=project.id,
                name="bootstrap-admin",
                key_hash=self.hash_key(raw),
                key_prefix=raw[:12],
                scopes_json=["admin"],
                created_at=now,
            )
            s.add(key)
            s.add(AuditLog(id=self._id("aud"), project_id=project.id, actor_key_prefix=None, action="admin.bootstrap", target_type="api_key", target_id=key.id, metadata_json={"email": email}))
            return {"project_id": project.id, "api_key_id": key.id, "api_key": raw, "key_prefix": key.key_prefix, "scopes": key.scopes_json}

    def create_project(self, name: str, org_id: str = "org_default", *, allowed_models: list[str] | None = None, ccr_enabled: bool | None = None) -> dict[str, Any]:
        with self.session() as s:
            if not s.get(Org, org_id):
                s.add(Org(id=org_id, name="Default Org" if org_id == "org_default" else org_id))
            p = Project(
                id=self._id("proj"),
                org_id=org_id,
                name=name,
                allowed_models_json=allowed_models,
                monthly_spend_cap_usd=self.settings.monthly_spend_usd,
                ccr_enabled=self.settings.ccr_enabled if ccr_enabled is None else ccr_enabled,
                ccr_retention_days=self.settings.ccr_retention_days,
            )
            s.add(p)
            return self.project_to_dict(p)

    def list_projects(self, org_id: str | None = None) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(Project).order_by(Project.created_at.desc())
            if org_id:
                stmt = stmt.where(Project.org_id == org_id)
            return [self.project_to_dict(p) for p in s.execute(stmt).scalars().all()]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            p = s.get(Project, project_id)
            return self.project_to_dict(p) if p else None

    @staticmethod
    def project_to_dict(p: Project) -> dict[str, Any]:
        return {
            "id": p.id,
            "org_id": p.org_id,
            "name": p.name,
            "allowed_models": p.allowed_models_json,
            "monthly_spend_cap_usd": p.monthly_spend_cap_usd,
            "ccr_enabled": p.ccr_enabled,
            "ccr_retention_days": p.ccr_retention_days,
            "tier": p.tier,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }

    # ── billing / licensing ───────────────────────────────────────────────────
    def get_project_tier(self, project_id: str, *, grace_days: int | None = None) -> str:
        """Cheap tier lookup for per-request enforcement; defaults to 'free'.

        Read-side backstop: if a paid period lapsed more than grace_days ago (e.g. a
        customer.subscription.deleted webhook was dropped/missed), self-heal to 'free' so a
        canceled project can't keep paid access indefinitely on stale state.
        """
        gd = grace_days if grace_days is not None else int(getattr(self.settings, "billing_grace_days", 3))
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p or not p.tier or p.tier == "free":
                return "free"
            pend = p.subscription_current_period_end
            if pend is not None:
                if pend.tzinfo is None:
                    pend = pend.replace(tzinfo=timezone.utc)
                if utcnow() > pend + timedelta(days=max(0, gd)):
                    return "free"
            return p.tier

    def get_project_billing(self, project_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return None
            return {
                "project_id": p.id,
                "tier": p.tier or "free",
                "subscription_status": p.subscription_status,
                "stripe_customer_id": p.stripe_customer_id,
                "stripe_subscription_id": p.stripe_subscription_id,
                "current_period_end": p.subscription_current_period_end.isoformat() if p.subscription_current_period_end else None,
            }

    def set_stripe_customer(self, *, project_id: str, customer_id: str) -> None:
        with self.session() as s:
            p = s.get(Project, project_id)
            if p and not p.stripe_customer_id:
                p.stripe_customer_id = customer_id

    def find_project_by_customer(self, customer_id: str) -> str | None:
        with self.session() as s:
            p = s.execute(select(Project).where(Project.stripe_customer_id == customer_id)).scalars().first()
            return p.id if p else None

    def apply_subscription(
        self,
        *,
        project_id: str,
        tier: str,
        status: str | None,
        stripe_customer_id: str | None = None,
        stripe_subscription_id: str | None = None,
        current_period_end: datetime | None = None,
    ) -> bool:
        """Idempotently apply a subscription state to a project — the license-issuance write.

        This is the ONLY place a tier is granted/revoked from a billing event. Returns False
        if the project doesn't exist.
        """
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return False
            p.tier = tier
            p.subscription_status = status
            if stripe_customer_id:
                # Never silently rebind a project to a DIFFERENT Stripe customer (cross-tenant /
                # mapping-corruption guard). First bind wins; a mismatch is left as-is.
                if not p.stripe_customer_id:
                    p.stripe_customer_id = stripe_customer_id
                elif p.stripe_customer_id != stripe_customer_id:
                    log.warning("refusing to rebind project %s from customer %s to %s",
                                project_id, p.stripe_customer_id, stripe_customer_id)
            if stripe_subscription_id is not None:
                p.stripe_subscription_id = stripe_subscription_id
            p.subscription_current_period_end = current_period_end
            return True

    # ── Device licensing (BYOK/self-hosted axis) ──────────────────────────────
    def hash_device_fingerprint(self, raw_fp: str) -> str:
        """HMAC the client-supplied device fingerprint with a DEDICATED pepper. The raw
        fingerprint is never stored; only this digest is used as the device identity."""
        return hmac.new(self.settings.device_fingerprint_salt.encode(), raw_fp.encode(), hashlib.sha256).hexdigest()

    def hash_license_key(self, raw_key: str) -> str:
        return hmac.new(self.settings.license_key_hash_salt.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _aware(dt: datetime | None) -> datetime | None:
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _license_live(self, lic: License, grace_days: int) -> bool:
        """Active status AND the paid period hasn't lapsed beyond grace (self-heal backstop)."""
        if lic.status != "active":
            return False
        pend = self._aware(lic.current_period_end)
        if pend is not None and utcnow() > pend + timedelta(days=max(0, grace_days)):
            return False
        return True

    @staticmethod
    def license_to_dict(lic: License) -> dict[str, Any]:
        return {
            "id": lic.id, "tier": lic.tier, "status": lic.status,
            "stripe_customer_id": lic.stripe_customer_id,
            "stripe_subscription_id": lic.stripe_subscription_id,
            "bound_device_hash": lic.bound_device_hash,
            "current_period_end": lic.current_period_end.isoformat() if lic.current_period_end else None,
            "issued_at": lic.issued_at.isoformat() if lic.issued_at else None,
        }

    def _grace(self) -> int:
        return int(getattr(self.settings, "billing_grace_days", 3))

    def get_active_license_for_device(self, device_hash: str | None) -> dict[str, Any] | None:
        if not device_hash:
            return None
        with self.session() as s:
            lic = s.execute(
                select(License).where(License.bound_device_hash == device_hash, License.status == "active")
                .order_by(License.issued_at.desc())
            ).scalars().first()
            if not lic or not self._license_live(lic, self._grace()):
                return None
            return self.license_to_dict(lic)

    def issue_license(self, *, stripe_checkout_session_id: str | None, stripe_customer_id: str | None,
                      stripe_subscription_id: str | None, tier: str = "pro",
                      current_period_end: datetime | None = None, device_hash: str | None = None) -> dict[str, Any]:
        """Mint a Pro license for a paid checkout session, EXACTLY once per session (the
        UNIQUE stripe_checkout_session_id is the single-redeem guarantee). On replay, returns
        the already-issued license (license_key=None) and optionally (re)binds the device.
        The raw key is returned only on first issue and never stored (only its HMAC)."""
        def _existing(sid: str) -> dict[str, Any] | None:
            with self.session() as s:
                lic = s.execute(select(License).where(License.stripe_checkout_session_id == sid)).scalar_one_or_none()
                if not lic:
                    return None
                if device_hash and lic.status == "active" and lic.bound_device_hash != device_hash:
                    lic.bound_device_hash = device_hash
                return {**self.license_to_dict(lic), "license_key": None, "already_issued": True}

        if stripe_checkout_session_id:
            prior = _existing(stripe_checkout_session_id)
            if prior:
                return prior
        raw = "DCMP-" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:32]
        lic_id = self._id("lic")
        try:
            with self.session() as s:
                lic = License(
                    id=lic_id, license_key_hash=self.hash_license_key(raw), tier=tier, status="active",
                    stripe_customer_id=stripe_customer_id, stripe_subscription_id=stripe_subscription_id,
                    stripe_checkout_session_id=stripe_checkout_session_id, bound_device_hash=device_hash,
                    current_period_end=current_period_end,
                )
                s.add(lic)
                s.add(AuditLog(id=self._id("aud"), project_id=None, actor_key_prefix=None,
                               action="license.issue", target_type="license", target_id=lic_id,
                               metadata_json={"tier": tier, "device_bound": bool(device_hash)}))
        except IntegrityError:
            # Concurrent redeem of the same session minted it first — return theirs, not a new key.
            if stripe_checkout_session_id:
                prior = _existing(stripe_checkout_session_id)
                if prior:
                    return prior
            raise
        return {"id": lic_id, "tier": tier, "status": "active", "license_key": raw,
                "current_period_end": current_period_end.isoformat() if current_period_end else None,
                "bound_device_hash": device_hash, "already_issued": False}

    def activate_device_binding(self, *, raw_key: str, device_hash: str) -> dict[str, Any]:
        """Bind (or rebind/migrate) an active license to this device. A license entitles exactly
        one device at a time, so rebinding implicitly releases the previous device."""
        key_hash = self.hash_license_key(raw_key)
        with self.session() as s:
            lic = s.execute(select(License).where(License.license_key_hash == key_hash)).scalar_one_or_none()
            if not lic:
                return {"ok": False, "reason": "invalid_key"}
            if lic.status != "active":
                return {"ok": False, "reason": "revoked"}
            if not self._license_live(lic, self._grace()):
                return {"ok": False, "reason": "expired"}
            lic.bound_device_hash = device_hash
            s.add(AuditLog(id=self._id("aud"), project_id=None, actor_key_prefix=None,
                           action="license.activate", target_type="license", target_id=lic.id,
                           metadata_json={"device_hash_prefix": device_hash[:12]}))
            return {"ok": True, "tier": lic.tier, "current_period_end": lic.current_period_end.isoformat() if lic.current_period_end else None, "license_id": lic.id}

    def release_device_binding(self, *, device_hash: str) -> int:
        with self.session() as s:
            rows = s.execute(select(License).where(License.bound_device_hash == device_hash, License.status == "active")).scalars().all()
            for lic in rows:
                lic.bound_device_hash = None
            return len(rows)

    def revoke_licenses_for_subscription(self, subscription_id: str | None) -> int:
        if not subscription_id:
            return 0
        with self.session() as s:
            rows = s.execute(select(License).where(License.stripe_subscription_id == subscription_id, License.status == "active")).scalars().all()
            for lic in rows:
                lic.status = "revoked"
                lic.revoked_at = utc_ts()
                lic.bound_device_hash = None
            return len(rows)

    def sync_licenses_for_subscription(self, *, subscription_id: str | None, status: str | None, current_period_end: datetime | None) -> int:
        """Sync period/status for a subscription's licenses (customer.subscription.updated)."""
        if not subscription_id:
            return 0
        with self.session() as s:
            rows = s.execute(select(License).where(License.stripe_subscription_id == subscription_id)).scalars().all()
            for lic in rows:
                lic.current_period_end = current_period_end
                if status in {"canceled", "unpaid", "incomplete_expired"}:
                    lic.status = "revoked"
                    lic.revoked_at = utc_ts()
                    lic.bound_device_hash = None
            return len(rows)

    def _quota_window(self, now: datetime | None = None) -> str:
        if getattr(self.settings, "free_quota_window", "daily") == "lifetime":
            return "lifetime"
        return (now or utcnow()).strftime("%Y-%m-%d")

    def try_consume_free_quota(self, device_hash: str, *, limit: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Atomically consume one free-tier compression for this device in the current window.
        Returns {consumed, used, limit, remaining}. The count is server-authoritative and resets
        per window (daily). consumed=False means the quota is spent -> caller degrades to mode=off.

        Concurrency-safe: a read-modify-write would lose updates under Postgres READ COMMITTED
        (parallel requests all read used<limit and all grant), so the increment is a single
        conditional UPDATE whose rowcount is the authoritative grant signal. The row lock means
        two parallel requests cannot both satisfy `compressions_used < limit`.
        """
        limit = int(self.settings.free_compression_quota) if limit is None else int(limit)
        wk = self._quota_window(now)
        # 1. Ensure the device's quota row exists (best-effort; tolerate the concurrent-insert race).
        try:
            with self.session() as s:
                if not s.execute(select(DeviceQuota.id).where(DeviceQuota.device_hash == device_hash)).first():
                    s.add(DeviceQuota(id=self._id("devq"), device_hash=device_hash, compressions_used=0, window_date=wk))
        except IntegrityError:
            pass  # another request created it first
        # 2. Reset a rolled-over window, then ATOMICALLY increment iff still under the limit.
        with self.session() as s:
            s.execute(
                update(DeviceQuota)
                .where(DeviceQuota.device_hash == device_hash, DeviceQuota.window_date != wk)
                .values(window_date=wk, compressions_used=0, updated_at=utc_ts())
            )
            res = s.execute(
                update(DeviceQuota)
                .where(DeviceQuota.device_hash == device_hash,
                       DeviceQuota.window_date == wk,
                       DeviceQuota.compressions_used < limit)
                .values(compressions_used=DeviceQuota.compressions_used + 1, updated_at=utc_ts())
            )
            consumed = (res.rowcount == 1)
            used = s.execute(select(DeviceQuota.compressions_used).where(DeviceQuota.device_hash == device_hash)).scalar_one_or_none() or 0
        used = int(used)
        return {"consumed": consumed, "used": used, "limit": limit, "remaining": max(0, limit - used)}

    def get_device_quota(self, device_hash: str, *, limit: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        limit = int(self.settings.free_compression_quota) if limit is None else int(limit)
        wk = self._quota_window(now)
        with self.session() as s:
            row = s.execute(select(DeviceQuota).where(DeviceQuota.device_hash == device_hash)).scalar_one_or_none()
            used = row.compressions_used if (row and row.window_date == wk) else 0
            return {"used": used, "limit": limit, "remaining": max(0, limit - used)}

    def get_device_license_status(self, device_hash: str | None) -> dict[str, Any]:
        """Combined device-scoped status for GET /v1/licenses/status (no quota consumption)."""
        if not device_hash:
            return {"tier": "free", "license_status": "none", "metered": False, **self.get_device_quota("")}
        lic = self.get_active_license_for_device(device_hash)
        if lic:
            return {"tier": lic["tier"], "license_status": "active", "current_period_end": lic.get("current_period_end"),
                    "quota_used": None, "quota_remaining": None, "free_quota_limit": None}
        q = self.get_device_quota(device_hash)
        return {"tier": "free", "license_status": "none", "current_period_end": None,
                "quota_used": q["used"], "quota_remaining": q["remaining"], "free_quota_limit": q["limit"]}

    def list_licenses(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Admin listing — never exposes the raw key (only the stored HMAC + metadata)."""
        with self.session() as s:
            rows = s.execute(select(License).order_by(License.issued_at.desc()).limit(limit)).scalars().all()
            out = []
            for lic in rows:
                d = self.license_to_dict(lic)
                d["bound_device_hash"] = (lic.bound_device_hash[:12] + "…") if lic.bound_device_hash else None
                d["stripe_checkout_session_id"] = lic.stripe_checkout_session_id
                out.append(d)
            return out

    def mark_stripe_event(self, event_id: str | None, event_type: str | None = None) -> bool:
        """Record a webhook event id once. Returns True if newly recorded (process it), False if
        already seen (skip) — prevents a redelivered event from double-issuing a license."""
        if not event_id:
            return True
        try:
            with self.session() as s:
                if s.get(StripeEvent, event_id):
                    return False
                s.add(StripeEvent(event_id=event_id, event_type=event_type))
        except IntegrityError:
            return False
        return True

    def create_api_key(self, *, project_id: str, name: str, scopes: list[str]) -> dict[str, Any]:
        if not scopes:
            # scopes=[] means no permissions — caller must be explicit about what
            # the key is allowed to do. Silently defaulting to basic scopes would
            # hide mis-configuration.
            raise ValueError("scopes must not be empty; provide at least one scope")
        raw = "dc_" + secrets.token_urlsafe(32)
        with self.session() as s:
            if not s.get(Project, project_id):
                raise KeyError(f"Project does not exist: {project_id}")
            k = ApiKey(id=self._id("key"), project_id=project_id, name=name, key_hash=self.hash_key(raw), key_prefix=raw[:12], scopes_json=scopes)
            s.add(k)
            s.add(AuditLog(id=self._id("aud"), project_id=project_id, actor_key_prefix=None, action="api_key.create", target_type="api_key", target_id=k.id, metadata_json={"name": name, "scopes": scopes}))
            public = self.api_key_to_dict(k)
            public["api_key"] = raw
            return public

    def list_api_keys(self, project_id: str) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(ApiKey).where(ApiKey.project_id == project_id).order_by(ApiKey.created_at.desc())).scalars().all()
            return [self.api_key_to_dict(k) for k in rows]

    def verify_api_key(self, raw_key: str) -> dict[str, Any] | None:
        if not raw_key:
            return None
        key_hash = self.hash_key(raw_key)
        with self.session() as s:
            k = s.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))).scalar_one_or_none()
            if not k:
                return None
            k.last_used_at = utc_ts()
            p = s.get(Project, k.project_id)
            return {
                "key_id": k.id,
                "project_id": k.project_id,
                "org_id": p.org_id if p else None,
                "key_prefix": k.key_prefix,
                "scopes": k.scopes_json or [],
            }

    def revoke_api_key(self, *, project_id: str, key_id: str, actor_prefix: str | None = None, request_id: str | None = None) -> bool:
        with self.session() as s:
            k = s.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project_id)).scalar_one_or_none()
            if not k:
                return False
            k.revoked_at = utc_ts()
            s.add(AuditLog(id=self._id("aud"), project_id=project_id, actor_key_prefix=actor_prefix, action="api_key.revoke", target_type="api_key", target_id=key_id, request_id=request_id, metadata_json={}))
            return True

    @staticmethod
    def api_key_to_dict(k: ApiKey) -> dict[str, Any]:
        return {
            "id": k.id,
            "project_id": k.project_id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scopes": k.scopes_json or [],
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }

    def record_audit(self, *, project_id: str | None, actor_key_prefix: str | None, action: str, target_type: str | None = None, target_id: str | None = None, request_id: str | None = None, ip_address: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        with self.session() as s:
            s.add(AuditLog(id=self._id("aud"), project_id=project_id, actor_key_prefix=actor_key_prefix, action=action, target_type=target_type, target_id=target_id, request_id=request_id, ip_address=ip_address, metadata_json=metadata or {}))

    def upsert_device(self, profile: dict[str, Any], *, project_id: str) -> dict[str, Any]:
        device_id = profile.get("id") or profile.get("device_id") or self._id("dev")
        profile = dict(profile)
        profile["id"] = device_id
        now = utc_ts()
        with self.session() as s:
            row = s.get(Device, device_id)
            if row and row.project_id == project_id:
                row.profile_json = profile
                row.updated_at = now
            else:
                s.merge(Device(id=device_id, project_id=project_id, profile_json=profile, created_at=now, updated_at=now))
            return profile

    def list_devices(self, *, project_id: str) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(Device).where(Device.project_id == project_id).order_by(Device.updated_at.desc())).scalars().all()
            return [d.profile_json for d in rows]

    def create_deployment(self, *, project_id: str, name: str, plan: dict[str, Any]) -> dict[str, Any]:
        with self.session() as s:
            existing = s.execute(select(Deployment).where(Deployment.project_id == project_id, Deployment.name == name)).scalar_one_or_none()
            if existing:
                existing.plan_json = plan
                row = existing
            else:
                row = Deployment(id=self._id("dep"), project_id=project_id, name=name, plan_json=plan)
                s.add(row)
            return {"id": row.id, "project_id": row.project_id, "name": row.name, "plan": row.plan_json, "created_at": row.created_at.isoformat() if row.created_at else None}

    def list_deployments(self, *, project_id: str) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(Deployment).where(Deployment.project_id == project_id).order_by(Deployment.created_at.desc())).scalars().all()
            return [{"id": d.id, "project_id": d.project_id, "name": d.name, "plan": d.plan_json, "created_at": d.created_at.isoformat() if d.created_at else None} for d in rows]

    def put_ccr(self, *, project_id: str, original_text: str, metadata: dict[str, Any] | None, crypto: CCRCrypto, scanner: SecretScanner, retention_days: int | None = None) -> dict[str, Any]:
        digest = hashlib.sha256((original_text or "").encode("utf-8", errors="ignore")).hexdigest()
        ccr_id = "ccr_" + digest[:32]
        findings = scanner.scan(original_text or "")
        expires = utc_ts() + timedelta(days=retention_days or self.settings.ccr_retention_days)
        meta = dict(metadata or {})
        meta["secret_findings"] = [f.to_dict() for f in findings]
        with self.session() as s:
            existing = s.get(CCRItem, ccr_id)
            if existing:
                return self.ccr_to_public(existing, include_payload=False)
            if findings:
                storage_mode = "redacted"
                payload = None
                key_id = None
                summary = scanner.summarize(original_text or "", findings)
                scan_status = "redacted_secret_detected"
            elif crypto.enabled:
                encrypted = crypto.encrypt(original_text or "")
                storage_mode = "encrypted"
                payload = encrypted.ciphertext
                key_id = encrypted.key_id
                summary = None
                scan_status = "passed"
            else:
                storage_mode = "metadata_only"
                payload = None
                key_id = None
                summary = f"CCR payload omitted because encryption key is not configured. sha256={digest} chars={len(original_text or '')}"
                scan_status = "not_stored_no_encryption_key"
            row = CCRItem(
                id=ccr_id,
                project_id=project_id,
                sha256=digest,
                encrypted_payload=payload,
                encryption_key_id=key_id,
                storage_mode=storage_mode,
                redacted_summary=summary,
                secret_scan_status=scan_status,
                metadata_json=meta,
                expires_at=expires,
            )
            s.add(row)
            return self.ccr_to_public(row, include_payload=False)

    def get_ccr(self, *, project_id: str, ccr_id: str, crypto: CCRCrypto, include_payload: bool = True) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.execute(select(CCRItem).where(CCRItem.id == ccr_id, CCRItem.project_id == project_id)).scalar_one_or_none()
            if not row:
                return None
            out = self.ccr_to_public(row, include_payload=False)
            if include_payload:
                if row.storage_mode == "encrypted" and row.encrypted_payload:
                    out["original_text"] = crypto.decrypt(row.encrypted_payload, row.encryption_key_id)
                else:
                    out["original_text"] = None
                    out["redacted_summary"] = row.redacted_summary
            return out

    def delete_ccr(self, *, project_id: str, ccr_id: str) -> bool:
        with self.session() as s:
            result = s.execute(delete(CCRItem).where(CCRItem.project_id == project_id, CCRItem.id == ccr_id))
            return bool(result.rowcount)

    def purge_expired_ccr(self) -> int:
        with self.session() as s:
            result = s.execute(delete(CCRItem).where(CCRItem.expires_at.is_not(None), CCRItem.expires_at < utc_ts()))
            return int(result.rowcount or 0)

    @staticmethod
    def ccr_to_public(row: CCRItem, *, include_payload: bool) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "sha256": row.sha256,
            "storage_mode": row.storage_mode,
            "secret_scan_status": row.secret_scan_status,
            "metadata": row.metadata_json or {},
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            **({"encrypted_payload_present": bool(row.encrypted_payload)} if not include_payload else {}),
        }

    def record_usage(self, event: dict[str, Any], *, project_id: str, key_prefix: str | None) -> dict[str, Any]:
        row = UsageEvent(
            id=self._id("use"),
            project_id=project_id,
            api_key_prefix=key_prefix,
            request_id=event.get("request_id"),
            upstream_request_id=event.get("upstream_request_id"),
            session_id=event.get("session_id"),
            endpoint=event.get("endpoint", "unknown"),
            model=event.get("model"),
            status_code=event.get("status_code"),
            latency_ms=event.get("latency_ms"),
            raw_input_tokens=int(event.get("raw_input_tokens", 0)),
            yscompress_input_tokens=int(event.get("yscompress_input_tokens", event.get("compressed_input_tokens", 0))),
            yellowstone_final_input_tokens=int(event.get("yellowstone_final_input_tokens", event.get("compressed_input_tokens", 0))),
            output_tokens=int(event.get("output_tokens", 0)),
            cache_creation_input_tokens=int(event.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(event.get("cache_read_input_tokens", 0)),
            tokens_saved=int(event.get("tokens_saved", 0)),
            billable_input_equivalent_tokens=int(event.get("billable_input_equivalent_tokens", 0)),
            estimated_cost_usd=float(event.get("estimated_cost_usd", 0.0)),
            estimated_savings_usd=float(event.get("estimated_savings_usd", 0.0)),
            compression_ratio=float(event.get("compression_ratio", 1.0)),
            workload_class=event.get("workload_class"),
            # Storage-boundary guard: only an allowlisted bucket is persisted; anything
            # else (e.g. a raw User-Agent from a future direct caller) becomes NULL.
            client=(event.get("client") if event.get("client") in CLIENT_BUCKETS else None),
            cache_mode=event.get("cache_mode"),
            route_json=event.get("route"),
            local_student_candidate=bool(event.get("local_student_candidate", False)),
            coding_intent=event.get("coding_intent"),
            served_by_student=bool(event.get("served_by_student", False)),
            optimization_steps_json=event.get("optimization_steps"),
            stream_status=event.get("stream_status"),
            stream_cancelled=bool(event.get("stream_cancelled", False)),
            stream_cancel_reason=event.get("stream_cancel_reason"),
            stream_json=event.get("stream_json"),
        )
        with self.session() as s:
            s.add(row)
            return self.usage_to_dict(row)

    # ── Teacher-student persistence (plan §"Governance At Store Boundary") ─────
    # The store is the privacy boundary: enum-like values are clamped to closed sets, and in the
    # default metadata_only mode NO prompt/answer/test content is written — only hashes, counts,
    # features, and route/verdict metadata. Content is persisted ONLY when capture is explicitly
    # enabled AND the resolved storage mode permits it.

    @staticmethod
    def _clamp(value: Any, allowed: frozenset[str], default: str) -> str:
        return value if value in allowed else default

    def effective_capture_mode(self, project_id: str) -> str:
        """Resolve the storage mode for a project (server AND project must allow; capture_locked forces
        metadata_only). Defaults to metadata_only (no content)."""
        return self._project_capture_policy(project_id)[0]

    def purge_expired_teacher_student_cases(self, *, now: datetime | None = None) -> int:
        """Hard-delete captured TS content whose retention window has passed (H2). Returns the count
        of rows whose content was cleared. Idempotent; safe to call from startup/maintenance."""
        cutoff = now or utcnow()
        cleared = 0
        with self.session() as s:
            rows = s.query(TeacherStudentCase).filter(TeacherStudentCase.expires_at.isnot(None)).all()
            for c in rows:
                exp = c.expires_at
                if exp is not None and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp is not None and exp < cutoff:
                    if c.student_answer is not None or c.teacher_answer is not None or c.final_answer is not None or c.tests_json is not None:
                        c.student_answer = c.teacher_answer = c.final_answer = c.tests_json = None
                        c.storage_mode = "metadata_only"
                        c.expires_at = None
                        cleared += 1
        return cleared

    def create_teacher_student_run(
        self, *, project_id: str, name: str | None = None, mode: str = "student_first",
        student_model: str | None = None, teacher_model: str | None = None,
        governance_mode: str = "metadata_only", metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = TeacherStudentRun(
            id=self._id("tsrun"),
            project_id=project_id,
            name=(name or None),
            mode=self._clamp(mode, TS_MODES, "student_first"),
            status="open",
            student_model=student_model,
            teacher_model=teacher_model,
            governance_mode=self._clamp(governance_mode, TS_GOVERNANCE_MODES, "metadata_only"),
            metadata_json=metadata,
        )
        with self.session() as s:
            s.add(row)
            return self.ts_run_to_dict(row)

    def _project_capture_policy(self, project_id: str) -> tuple[str, int]:
        """Return (effective_storage_mode, retention_days) resolved from server + project policy.
        capture_locked or server-disabled forces metadata_only; retention comes from the PROJECT
        column (falling back to the server default) so per-project retention is honored (H2)."""
        if not self.settings.teacher_student_capture_enabled:
            return "metadata_only", int(self.settings.teacher_student_capture_retention_days)
        with self.session() as s:
            proj = s.get(Project, project_id)
            if proj is None or proj.teacher_student_capture_locked or not proj.teacher_student_capture_enabled:
                return "metadata_only", int(self.settings.teacher_student_capture_retention_days)
            mode = self._clamp(proj.teacher_student_governance_mode, TS_GOVERNANCE_MODES, "metadata_only")
            retention = int(proj.teacher_student_capture_retention_days or self.settings.teacher_student_capture_retention_days)
            return mode, max(1, retention)

    def _scrub_for_storage(self, text: str | None) -> tuple[str | None, str]:
        """Scrub secrets/PII from content before it is persisted (H1). Returns (scrubbed, scan_status).
        Mirrors the CCR scan-before-store pattern."""
        if text is None:
            return None, "no_content"
        scanner = SecretScanner(mode="redact")
        findings = scanner.scan(text)
        scrubbed = scanner.scrub(text)
        return scrubbed, ("redacted_secret_detected" if findings else "passed")

    def record_teacher_student_case(
        self, case: dict[str, Any], *, project_id: str, run_id: str | None = None, request_capture: bool = False,
    ) -> dict[str, Any]:
        """Persist a case + its verdict, enforcing storage mode at the boundary.

        Content (prompt/answers/tests) is stored ONLY when THREE gates all allow it (H1):
          1. server capture enabled, 2. project capture enabled (and not locked) with mode='redacted',
          3. the request itself opted in (request_capture=True).
        When stored, content is scrubbed of secrets/PII first and a scan status is recorded. In every
        other case content is dropped here, so a caller may pass content freely and the store
        guarantees it is never persisted by accident."""
        mode, retention_days = self._project_capture_policy(project_id)
        # The request-level opt-in is a required third gate: capture:false means metadata-only even if
        # server+project allow it.
        store_content = mode == "redacted" and bool(request_capture)
        effective_mode = mode if store_content else ("regulated" if mode == "regulated" else "metadata_only")
        expires_at = utcnow() + timedelta(days=retention_days) if store_content else None

        scan_status = "no_content"
        if store_content:
            student_a, ss1 = self._scrub_for_storage(case.get("student_answer"))
            teacher_a, ss2 = self._scrub_for_storage(case.get("teacher_answer"))
            final_a, ss3 = self._scrub_for_storage(case.get("final_answer"))
            tests_j = case.get("tests_json")  # tests are user-authored asserts; kept as-is under redacted
            scan_status = "redacted_secret_detected" if "redacted_secret_detected" in {ss1, ss2, ss3} else "passed"
        else:
            student_a = teacher_a = final_a = tests_j = None

        # Salted prompt hash (M4): a raw SHA-256 of short prompts is dictionary-attackable. HMAC with
        # the project-hash pepper makes the digest a non-reversible correlator. Omitted in regulated mode.
        prompt_sha = None
        raw_sha = case.get("prompt_sha256")
        if raw_sha and effective_mode != "regulated":
            prompt_sha = hmac.new(self.settings.api_key_hash_salt.encode(), raw_sha.encode(), hashlib.sha256).hexdigest()

        verdict = case.get("verdict") or {}
        feature = dict(case.get("feature_json") or {})
        feature["secret_scan_status"] = scan_status
        row = TeacherStudentCase(
            id=self._id("tscase"),
            run_id=run_id,
            project_id=project_id,
            usage_event_id=case.get("usage_event_id"),
            request_id=case.get("request_id"),
            session_id=case.get("session_id"),
            client=(case.get("client") if case.get("client") in CLIENT_BUCKETS else None),
            route=self._clamp(case.get("route"), TS_ROUTES, "student_unavailable"),
            workload_class=case.get("workload_class"),
            coding_intent=case.get("coding_intent"),
            escalation_reason=case.get("escalation_reason"),
            storage_mode=effective_mode,
            prompt_sha256=prompt_sha,
            feature_json=feature,
            # Content is gated by all three gates AND scrubbed — never trust the caller alone.
            student_answer=student_a,
            teacher_answer=teacher_a,
            final_answer=final_a,
            tests_json=tests_j,
            input_tokens=int(case.get("input_tokens", 0)),
            student_output_tokens=int(case.get("student_output_tokens", 0)),
            teacher_output_tokens=int(case.get("teacher_output_tokens", 0)),
            student_latency_ms=int(case.get("student_latency_ms", 0)),
            teacher_latency_ms=int(case.get("teacher_latency_ms", 0)),
            expires_at=expires_at,
        )
        with self.session() as s:
            s.add(row)
            s.flush()
            vrow = TeacherStudentVerdict(
                id=self._id("tsverd"),
                case_id=row.id,
                verifier_version=verdict.get("verifier_version"),
                decision=self._clamp(verdict.get("decision"), TS_DECISIONS, "escalate"),
                confidence=float(verdict.get("confidence", 0.0)),
                threshold=float(verdict.get("threshold", 0.0)),
                escalated=bool(verdict.get("escalated", verdict.get("decision") == "escalate")),
                escalation_reason=verdict.get("escalation_reason"),
                tests_ran=bool(verdict.get("tests_ran", False)),
                tests_passed=verdict.get("tests_passed"),
                sandbox_backend=verdict.get("sandbox_backend"),
                judge_model=verdict.get("judge_model"),
                layers_json=verdict.get("layers"),
            )
            s.add(vrow)
            if run_id is not None:
                run = s.get(TeacherStudentRun, run_id)
                if run is not None:
                    run.case_count = (run.case_count or 0) + 1
                    if case.get("route") in {"teacher_corrected", "teacher_only"}:
                        run.teacher_call_count = (run.teacher_call_count or 0) + 1
            s.flush()
            return self.ts_case_to_dict(row, vrow)

    def list_teacher_student_runs(self, *, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = (
                s.query(TeacherStudentRun)
                .filter(TeacherStudentRun.project_id == project_id)
                .order_by(TeacherStudentRun.created_at.desc())
                .limit(limit)
                .all()
            )
            return [self.ts_run_to_dict(r) for r in rows]

    def teacher_student_summary(self, *, project_id: str) -> dict[str, Any]:
        """Counts-only deflection summary for the dashboard (no content).

        Beyond the route breakdown, this surfaces the VERIFICATION-SIGNAL breakdown of accepted
        (student_only) cases — needed for the serve-canary: a tests-backed accept (executed sandbox
        tests, the strongest 1.0 evidence tier) is more trustworthy than a judge-only accept (0.85,
        no executed tests), and an operator must be able to watch the judge-only rate during ramp."""
        with self.session() as s:
            cases = s.query(TeacherStudentCase).filter(TeacherStudentCase.project_id == project_id).all()
            total = len(cases)
            by_route: dict[str, int] = {}
            teacher_calls = 0
            case_ids: list[str] = []
            for c in cases:
                by_route[c.route] = by_route.get(c.route, 0) + 1
                if c.route in {"teacher_corrected", "teacher_only"}:
                    teacher_calls += 1
                case_ids.append(c.id)
            accepted = by_route.get("student_only", 0)

            # Join verdicts to classify HOW each accept was verified + the judge pass-rate.
            tests_backed = judge_only = no_evidence_accepts = judge_consulted = judge_passed = 0
            confidences: list[float] = []
            if case_ids:
                verdicts = (
                    s.query(TeacherStudentVerdict)
                    .filter(TeacherStudentVerdict.case_id.in_(case_ids))
                    .all()
                )
                # Map case_id -> route for accept classification.
                route_by_case = {c.id: c.route for c in cases}
                for v in verdicts:
                    if v.judge_model is not None:
                        judge_consulted += 1
                        if not v.escalated:
                            judge_passed += 1
                    if route_by_case.get(v.case_id) == "student_only":
                        confidences.append(v.confidence)
                        if v.tests_ran and v.tests_passed:
                            tests_backed += 1
                        elif v.judge_model is not None:
                            judge_only += 1
                        else:
                            # Should be unreachable (no-evidence cap can't clear threshold); track it.
                            no_evidence_accepts += 1

            avg_conf = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
            return {
                "cases": total,
                "student_accepted": accepted,
                "teacher_called": teacher_calls,
                "escalation_rate": round((total - accepted) / total, 4) if total else 0.0,
                "by_route": by_route,
                # Verification-signal breakdown of the accepts (serve-canary observability).
                "accept_evidence": {
                    "tests_backed": tests_backed,      # executed sandbox tests passed (cap 1.0)
                    "judge_only": judge_only,          # independent judge passed, no tests (cap 0.85)
                    "no_evidence": no_evidence_accepts,  # invariant guard: should always be 0
                    "avg_confidence": avg_conf,
                },
                "judge": {
                    "consulted": judge_consulted,
                    "passed": judge_passed,
                    "pass_rate": round(judge_passed / judge_consulted, 4) if judge_consulted else 0.0,
                },
            }

    def export_distillation_records(
        self, *, project_id: str, target: str = "sft", limit: int = 1000,
        allow_claude_targets: bool = False, claude_targets_signoff: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build distillation export records from STORED cases (Phase 7). Returns (records, meta).

        Hard governance rules enforced here:
          * Only cases that actually have stored content are exportable. Metadata-only / regulated
            cases have no content and are silently excluded (reported in meta.skipped_no_content).
          * `target="sft"` exports the FINAL accepted answer as the label. When the final answer came
            from the Claude TEACHER (route teacher_*), it is a Claude output — excluded unless BOTH the
            allow flag AND the legal/product sign-off are set (M3). Enforcing both AT THE DATA BOUNDARY
            means even a direct store caller (script/test) cannot exfiltrate Claude labels without the
            sign-off — production validation is no longer the only gate.
          * Expired cases (past retention) are excluded.
        """
        from .models import TeacherStudentCase

        claude_targets_ok = bool(allow_claude_targets and claude_targets_signoff)
        records: list[dict[str, Any]] = []
        skipped_no_content = 0
        skipped_claude_target = 0
        skipped_expired = 0
        now = utcnow()
        with self.session() as s:
            rows = (
                s.query(TeacherStudentCase)
                .filter(TeacherStudentCase.project_id == project_id)
                .order_by(TeacherStudentCase.created_at.desc())
                .limit(limit)
                .all()
            )
            for c in rows:
                # SQLite returns naive datetimes; Postgres returns tz-aware. Normalize before compare
                # so the same code path works on both (mirrors support_router.parse_timestamp).
                exp = c.expires_at
                if exp is not None and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp is not None and exp < now:
                    skipped_expired += 1
                    continue
                if c.final_answer is None and c.student_answer is None:
                    skipped_no_content += 1
                    continue
                # Is the exportable label a Claude (teacher) output?
                is_claude_label = c.route in {"teacher_corrected", "teacher_only"}
                if is_claude_label and not claude_targets_ok:
                    skipped_claude_target += 1
                    continue
                label = c.final_answer or c.student_answer
                records.append({
                    "id": c.id,
                    "prompt_sha256": c.prompt_sha256,
                    "completion": label,
                    "route": c.route,
                    "label_source": "teacher" if is_claude_label else "student",
                })
        meta = {
            "target": target,
            "exported": len(records),
            "skipped_no_content": skipped_no_content,
            "skipped_claude_target": skipped_claude_target,
            "skipped_expired": skipped_expired,
            "allow_claude_targets": allow_claude_targets,
            "claude_targets_signoff": claude_targets_signoff,
            "claude_targets_exported": claude_targets_ok,
        }
        return records, meta

    @staticmethod
    def ts_run_to_dict(r: TeacherStudentRun) -> dict[str, Any]:
        return {
            "id": r.id, "project_id": r.project_id, "name": r.name, "mode": r.mode, "status": r.status,
            "student_model": r.student_model, "teacher_model": r.teacher_model,
            "governance_mode": r.governance_mode, "case_count": r.case_count,
            "teacher_call_count": r.teacher_call_count, "metadata": r.metadata_json,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    @staticmethod
    def ts_case_to_dict(c: TeacherStudentCase, v: TeacherStudentVerdict | None = None) -> dict[str, Any]:
        out = {
            "id": c.id, "run_id": c.run_id, "project_id": c.project_id, "route": c.route,
            "storage_mode": c.storage_mode, "escalation_reason": c.escalation_reason,
            "coding_intent": c.coding_intent, "prompt_sha256": c.prompt_sha256,
            "input_tokens": c.input_tokens, "student_output_tokens": c.student_output_tokens,
            "teacher_output_tokens": c.teacher_output_tokens,
            "has_content": c.student_answer is not None or c.final_answer is not None,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        }
        if v is not None:
            out["verdict"] = {
                "decision": v.decision, "confidence": v.confidence, "threshold": v.threshold,
                "escalated": v.escalated, "escalation_reason": v.escalation_reason,
                "tests_ran": v.tests_ran, "tests_passed": v.tests_passed,
            }
        return out

    @staticmethod
    def _row_savings_pct(raw_tokens: int, final_tokens: int) -> float:
        """Per-request savings %, floored at 0 (gateway never sends more than raw)."""
        if not raw_tokens:
            return 0.0
        return max(0.0, (1.0 - final_tokens / raw_tokens) * 100.0)

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def usage_summary(self, *, project_id: str, since: datetime | None = None) -> dict[str, Any]:
        def scope(query):
            query = query.filter(UsageEvent.project_id == project_id)
            if since is not None:
                query = query.filter(UsageEvent.created_at >= since)
            return query

        with self.session() as s:
            q = scope(s.query(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.raw_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yscompress_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yellowstone_final_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.tokens_saved), 0),
                func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
                func.coalesce(func.sum(UsageEvent.estimated_savings_usd), 0.0),
            ))
            row = q.one()
            by_model = scope(s.query(UsageEvent.model, func.count(UsageEvent.id), func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0))).group_by(UsageEvent.model).all()

            # Per-request (raw, final) pairs for latest + ROLLING MEDIAN, newest first.
            # id.desc() is a deterministic tiebreaker so "latest" can't flip between
            # dashboard loads when two rows share a created_at (Python-clock ties).
            # Capped at the latest 50 so the headline median is a rolling median of the most
            # recent 50 requests (reflects current performance, not diluted by old history).
            per_request = scope(s.query(
                UsageEvent.raw_input_tokens,
                UsageEvent.yellowstone_final_input_tokens,
            )).order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).limit(ROLLING_MEDIAN_WINDOW).all()

            # By-workload aggregation.
            by_workload_rows = scope(s.query(
                UsageEvent.workload_class,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.raw_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yellowstone_final_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.tokens_saved), 0),
            )).group_by(UsageEvent.workload_class).all()

            # By-client (harness) aggregation — splits savings across claude_code /
            # openclaw / sdk / other / unknown. Carries cost + $ saved so a reader can
            # see which harness drives spend, not just request volume.
            by_client_rows = scope(s.query(
                UsageEvent.client,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.raw_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yellowstone_final_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.tokens_saved), 0),
                func.coalesce(func.sum(UsageEvent.estimated_savings_usd), 0.0),
            )).group_by(UsageEvent.client).all()

        requests, raw, ys, final, out, saved, cost, savings = row
        raw = int(raw or 0); final = int(final or 0); ys = int(ys or 0)

        per_savings = [self._row_savings_pct(int(r or 0), int(f or 0)) for r, f in per_request]
        latest_savings = per_savings[0] if per_savings else 0.0
        median_savings = self._median(per_savings)

        by_workload = []
        for wc, c, wraw, wfinal, wsaved in by_workload_rows:
            wraw = int(wraw or 0); wfinal = int(wfinal or 0)
            by_workload.append({
                "workload_class": wc or "unclassified",
                "requests": int(c),
                "raw_input_tokens": wraw,
                "sent_input_tokens": wfinal,
                "tokens_saved": int(wsaved or 0),
                "savings_percent": self._row_savings_pct(wraw, wfinal),
            })
        by_workload.sort(key=lambda x: x["tokens_saved"], reverse=True)

        by_client = []
        for cl, c, craw, cfinal, csaved, cusd in by_client_rows:
            craw = int(craw or 0); cfinal = int(cfinal or 0)
            by_client.append({
                "client": cl or "unknown",   # NULL (pre-tagging / no UA) reads as 'unknown'
                "requests": int(c),
                "raw_input_tokens": craw,
                "sent_input_tokens": cfinal,
                "tokens_saved": int(csaved or 0),
                "estimated_savings_usd": round(float(cusd or 0.0), 6),
                "savings_percent": self._row_savings_pct(craw, cfinal),
            })
        by_client.sort(key=lambda x: x["tokens_saved"], reverse=True)

        return {
            "requests": int(requests or 0),
            "raw_input_tokens": raw,
            "yscompress_input_tokens": ys,
            "yellowstone_final_input_tokens": final,
            "sent_input_tokens": final,
            "output_tokens": int(out or 0),
            "tokens_saved": int(saved or 0),
            # Headline metrics (Hina's decision): latest first, median second,
            # lifetime as cost/value proof. NOT a cumulative average headline.
            "latest_savings_percent": round(latest_savings, 1),
            "median_savings_percent": round(median_savings, 1),
            # Lifetime aggregate kept for cost/value proof, floored at 0.
            "lifetime_savings_percent": round(max(0.0, (1.0 - final / raw) * 100) if raw else 0.0, 1),
            "compression_ratio": min(1.0, final / raw) if raw else 1.0,
            # Back-compat alias (older callers/tests read estimated_savings_percent).
            "estimated_savings_percent": round(max(0.0, (1.0 - final / raw) * 100) if raw else 0.0, 1),
            "estimated_cost_usd": round(float(cost or 0.0), 6),
            "estimated_savings_usd": round(float(savings or 0.0), 6),
            "by_model": [{"model": m, "requests": int(c), "estimated_cost_usd": round(float(v or 0.0), 6)} for m, c, v in by_model],
            "by_workload": by_workload,
            "by_client": by_client,
        }

    def recent_usage(self, *, project_id: str, limit: int = 8, since: datetime | None = None) -> list[dict[str, Any]]:
        """Most recent usage events, newest first — for the dashboard feed."""
        with self.session() as s:
            stmt = select(UsageEvent).where(UsageEvent.project_id == project_id)
            if since is not None:
                stmt = stmt.where(UsageEvent.created_at >= since)
            stmt = stmt.order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).limit(limit)
            return [self.usage_to_dict(u) for u in s.execute(stmt).scalars().all()]

    def usage_savings_timeseries(
        self,
        *,
        project_id: str,
        since: datetime | None,
        until: datetime | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        """The most recent `limit` requests in the window [since, until], each as its own
        point (NOT time-bucketed), for the performance-over-time chart. Ordered oldest ->
        newest, so the LAST point is always the latest request.

        Why per-request instead of time-bucketing: Claude Code fires a burst of requests
        per turn (the agent loop), all within seconds. Bucketing them into a time slice and
        plotting the slice MEAN averaged the burst into one diluted value (e.g. four log
        reads at 34/29/4/6% -> 18%), so the chart's right edge could never equal the
        headline "Latest request" %. One point per request fixes that: the end point IS the
        latest request, and savings use the SAME calc as the headline (`_row_savings_pct`),
        so the two always agree. Stable on reload — the same recent requests render
        identically until a new one arrives. Returns
        {"points": [ {"saved_pct": float, "created_at": datetime} ] oldest->newest,
         "t_start", "t_end"} (t_start/t_end = first/last point time, for axis labels)."""
        until = until or utc_ts()
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        with self.session() as s:
            q = s.query(
                UsageEvent.created_at,
                UsageEvent.raw_input_tokens,
                UsageEvent.yellowstone_final_input_tokens,
            ).filter(UsageEvent.project_id == project_id, UsageEvent.created_at <= until)
            if since is not None:
                q = q.filter(UsageEvent.created_at >= since)
            # Newest first (id.desc tiebreaker = same "latest" as the headline card), capped,
            # then reversed to oldest->newest for left-to-right plotting.
            rows = q.order_by(
                UsageEvent.created_at.desc(), UsageEvent.id.desc()
            ).limit(max(1, limit)).all()

        if not rows:
            return {"points": [], "t_start": None, "t_end": None}

        def _aware(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        rows = list(reversed(rows))  # oldest -> newest
        points = [
            {"saved_pct": self._row_savings_pct(int(raw or 0), int(final or 0)),
             "created_at": _aware(created)}
            for created, raw, final in rows
        ]
        return {"points": points, "t_start": points[0]["created_at"], "t_end": points[-1]["created_at"]}

    def monthly_spend(self, *, project_id: str, now: datetime | None = None) -> float:
        now = now or utc_ts()
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        with self.session() as s:
            val = s.query(func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0)).filter(UsageEvent.project_id == project_id, UsageEvent.created_at >= start).scalar()
            return float(val or 0.0)

    def export_usage(self, *, project_id: str, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(UsageEvent).where(UsageEvent.project_id == project_id).order_by(UsageEvent.created_at.asc())
            if start:
                stmt = stmt.where(UsageEvent.created_at >= start)
            if end:
                stmt = stmt.where(UsageEvent.created_at <= end)
            return [self.usage_to_dict(u) for u in s.execute(stmt).scalars().all()]

    @staticmethod
    def usage_to_dict(u: UsageEvent) -> dict[str, Any]:
        return {
            "id": u.id,
            "project_id": u.project_id,
            "api_key_prefix": u.api_key_prefix,
            "request_id": u.request_id,
            "upstream_request_id": u.upstream_request_id,
            "session_id": u.session_id,
            "endpoint": u.endpoint,
            "model": u.model,
            "status_code": u.status_code,
            "latency_ms": u.latency_ms,
            "raw_input_tokens": u.raw_input_tokens,
            "yscompress_input_tokens": u.yscompress_input_tokens,
            "yellowstone_final_input_tokens": u.yellowstone_final_input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": u.cache_creation_input_tokens,
            "cache_read_input_tokens": u.cache_read_input_tokens,
            "tokens_saved": u.tokens_saved,
            "billable_input_equivalent_tokens": u.billable_input_equivalent_tokens,
            "estimated_cost_usd": u.estimated_cost_usd,
            "estimated_savings_usd": u.estimated_savings_usd,
            "compression_ratio": u.compression_ratio,
            "workload_class": u.workload_class,
            "client": u.client,
            "cache_mode": u.cache_mode,
            "route": u.route_json,
            "optimization_steps": u.optimization_steps_json,
            "stream_status": u.stream_status,
            "stream_cancelled": bool(u.stream_cancelled),
            "stream_cancel_reason": u.stream_cancel_reason,
            "stream_json": u.stream_json,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }

    def support_bundle_snapshot(self) -> dict[str, Any]:
        """Metadata-only store snapshot for the support bundle.

        Deliberately returns ONLY counts and aggregate token/cost totals — never
        rows, prompts, keys, CCR payloads, or PII. The support bundle scrubber runs
        over this too, but by construction there is nothing sensitive to scrub.
        """
        with self.session() as s:
            counts = {
                "orgs": s.query(func.count(Org.id)).scalar() or 0,
                "projects": s.query(func.count(Project.id)).scalar() or 0,
                "api_keys": s.query(func.count(ApiKey.id)).scalar() or 0,
                "api_keys_active": s.query(func.count(ApiKey.id)).filter(ApiKey.revoked_at.is_(None)).scalar() or 0,
                "devices": s.query(func.count(Device.id)).scalar() or 0,
                "ccr_items": s.query(func.count(CCRItem.id)).scalar() or 0,
                "usage_events": s.query(func.count(UsageEvent.id)).scalar() or 0,
                "audit_events": s.query(func.count(AuditLog.id)).scalar() or 0,
            }
            usage = s.query(
                func.coalesce(func.sum(UsageEvent.raw_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yellowstone_final_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.tokens_saved), 0),
                func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
                func.coalesce(func.sum(UsageEvent.estimated_savings_usd), 0.0),
                func.count(func.distinct(UsageEvent.model)),
            ).one()
            stream_cancelled = s.query(func.count(UsageEvent.id)).filter(UsageEvent.stream_cancelled.is_(True)).scalar() or 0
        raw, final, out, saved, cost, savings, distinct_models = usage
        return {
            "dialect": self.engine.dialect.name,
            "counts": counts,
            "usage_totals": {
                "raw_input_tokens": int(raw or 0),
                "final_input_tokens": int(final or 0),
                "output_tokens": int(out or 0),
                "tokens_saved": int(saved or 0),
                "estimated_cost_usd": round(float(cost or 0.0), 6),
                "estimated_savings_usd": round(float(savings or 0.0), 6),
                "distinct_models": int(distinct_models or 0),
                "cancelled_streams": int(stream_cancelled),
            },
        }

    def support_session_report(
        self,
        *,
        project_id: str,
        session_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        """Privacy-safe per-session diagnostics for a support bundle.

        Returns ONLY metadata — request IDs, endpoints, models, status codes, token
        counts, latencies, stream status. Usage rows store no prompt/response content,
        so there is nothing here to leak; the support-bundle scrubber runs over it anyway.
        """
        with self.session() as s:
            q = s.query(UsageEvent).filter(
                UsageEvent.project_id == project_id, UsageEvent.session_id == session_id
            )
            if since is not None:
                q = q.filter(UsageEvent.created_at >= since)
            if until is not None:
                q = q.filter(UsageEvent.created_at <= until)
            rows = q.order_by(UsageEvent.created_at.asc()).limit(limit).all()

        requests = [
            {
                "request_id": r.request_id,
                "upstream_request_id": r.upstream_request_id,
                "endpoint": r.endpoint,
                "model": r.model,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "raw_input_tokens": r.raw_input_tokens,
                "final_input_tokens": r.yellowstone_final_input_tokens,
                "output_tokens": r.output_tokens,
                "tokens_saved": r.tokens_saved,
                "estimated_cost_usd": r.estimated_cost_usd,
                "workload_class": r.workload_class,
                "stream_status": r.stream_status,
                "stream_cancelled": bool(r.stream_cancelled),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

        def tally(field: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for x in requests:
                k = str(x.get(field))
                out[k] = out.get(k, 0) + 1
            return out

        summary = {
            "session_id": session_id,
            "requests": len(requests),
            "truncated": len(requests) >= limit,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "first_at": requests[0]["created_at"] if requests else None,
            "last_at": requests[-1]["created_at"] if requests else None,
            "raw_input_tokens": sum(x["raw_input_tokens"] or 0 for x in requests),
            "final_input_tokens": sum(x["final_input_tokens"] or 0 for x in requests),
            "output_tokens": sum(x["output_tokens"] or 0 for x in requests),
            "tokens_saved": sum(x["tokens_saved"] or 0 for x in requests),
            "estimated_cost_usd": round(sum(x["estimated_cost_usd"] or 0.0 for x in requests), 6),
            "cancelled_streams": sum(1 for x in requests if x["stream_cancelled"]),
            "by_endpoint": tally("endpoint"),
            "by_model": tally("model"),
            "by_status": tally("status_code"),
        }
        return {"summary": summary, "requests": requests}

    def list_audit_log(self, *, project_id: str, action: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(AuditLog).where(AuditLog.project_id == project_id).order_by(AuditLog.created_at.desc()).limit(limit)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            rows = s.execute(stmt).scalars().all()
            return [{"id": r.id, "project_id": r.project_id, "actor_key_prefix": r.actor_key_prefix, "action": r.action, "target_type": r.target_type, "target_id": r.target_id, "request_id": r.request_id, "metadata": r.metadata_json, "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
