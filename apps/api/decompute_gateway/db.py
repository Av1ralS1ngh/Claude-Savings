from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any, Iterator

from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .crypto import CCRCrypto
from .models import ApiKey, AuditLog, Base, CCRItem, Deployment, Device, Membership, Org, Project, UsageEvent, User, utcnow
from .secret_scanner import SecretScanner


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
    def _alembic_head() -> str:
        """Current Alembic head revision, so the SQLite create_all path stamps the
        same version `alembic upgrade head` would. Resolves the script location to an
        ABSOLUTE path so head detection works regardless of process CWD (gunicorn/
        systemd often run from / or a unit WorkingDirectory)."""
        from pathlib import Path
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        ini = Path(__file__).resolve().parents[3] / "alembic.ini"
        cfg = Config(str(ini)) if ini.exists() else Config("alembic.ini")
        # script_location in alembic.ini is relative ("alembic"); make it absolute so
        # ScriptDirectory doesn't resolve against CWD and silently fail.
        script_dir = (ini.parent / "alembic")
        if script_dir.exists():
            cfg.set_main_option("script_location", str(script_dir))
        return ScriptDirectory.from_config(cfg).get_current_head()

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
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }

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
            cache_mode=event.get("cache_mode"),
            route_json=event.get("route"),
            optimization_steps_json=event.get("optimization_steps"),
            stream_status=event.get("stream_status"),
            stream_cancelled=bool(event.get("stream_cancelled", False)),
            stream_cancel_reason=event.get("stream_cancel_reason"),
            stream_json=event.get("stream_json"),
        )
        with self.session() as s:
            s.add(row)
            return self.usage_to_dict(row)

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

            # Per-request (raw, final) pairs for latest + median, newest first.
            # id.desc() is a deterministic tiebreaker so "latest" can't flip between
            # dashboard loads when two rows share a created_at (Python-clock ties).
            per_request = scope(s.query(
                UsageEvent.raw_input_tokens,
                UsageEvent.yellowstone_final_input_tokens,
            )).order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).all()

            # By-workload aggregation.
            by_workload_rows = scope(s.query(
                UsageEvent.workload_class,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.raw_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.yellowstone_final_input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.tokens_saved), 0),
            )).group_by(UsageEvent.workload_class).all()

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
        }

    def recent_usage(self, *, project_id: str, limit: int = 8, since: datetime | None = None) -> list[dict[str, Any]]:
        """Most recent usage events, newest first — for the dashboard feed."""
        with self.session() as s:
            stmt = select(UsageEvent).where(UsageEvent.project_id == project_id)
            if since is not None:
                stmt = stmt.where(UsageEvent.created_at >= since)
            stmt = stmt.order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).limit(limit)
            return [self.usage_to_dict(u) for u in s.execute(stmt).scalars().all()]

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
            "cache_mode": u.cache_mode,
            "route": u.route_json,
            "optimization_steps": u.optimization_steps_json,
            "stream_status": u.stream_status,
            "stream_cancelled": bool(u.stream_cancelled),
            "stream_cancel_reason": u.stream_cancel_reason,
            "stream_json": u.stream_json,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }

    def list_audit_log(self, *, project_id: str, action: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(AuditLog).where(AuditLog.project_id == project_id).order_by(AuditLog.created_at.desc()).limit(limit)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            rows = s.execute(stmt).scalars().all()
            return [{"id": r.id, "project_id": r.project_id, "actor_key_prefix": r.actor_key_prefix, "action": r.action, "target_type": r.target_type, "target_id": r.target_id, "request_id": r.request_id, "metadata": r.metadata_json, "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
