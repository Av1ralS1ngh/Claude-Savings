"""
P0 Acceptance Test — Tenant Isolation

Creates two projects (A and B), two API keys, two CCR records, and proves
project A cannot read project B's usage, CCR, deployments, or devices.
Also verifies scopes, revocation, and audit log coverage.
"""
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from decompute_gateway.config import Settings
from decompute_gateway.crypto import CCRCrypto
from decompute_gateway.db import Store
from decompute_gateway.secret_scanner import SecretScanner
from decompute_gateway.security import AuthPrincipal


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/iso.db",
        ccr_encryption_key=Fernet.generate_key().decode(),
        ccr_enabled=True,
        ccr_retention_days=7,
    )
    s = Store(settings)
    s.create_schema_for_local_dev()
    return s


@pytest.fixture()
def crypto(store):
    return CCRCrypto(store.settings.ccr_encryption_key, store.settings.ccr_key_id)


@pytest.fixture()
def scanner():
    return SecretScanner()


@pytest.fixture()
def two_projects(store):
    proj_a = store.create_project("Project Alpha")
    proj_b = store.create_project("Project Beta")
    return proj_a, proj_b


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_project(store, crypto, scanner, project_id: str, label: str) -> dict:
    """Seed one device, deployment, CCR record, and usage event into project."""
    store.upsert_device({"id": f"dev-{label}", "name": f"{label} device", "kind": "cpu", "memory_gb": 8, "compute_score": 1.0, "runtimes": ["mock"]}, project_id=project_id)
    store.create_deployment(project_id=project_id, name=f"deploy-{label}", plan={"route": "anthropic_claude"})
    ccr = store.put_ccr(project_id=project_id, original_text=f"secret log from {label}", metadata={"label": label}, crypto=crypto, scanner=scanner)
    store.record_usage({"endpoint": "/v1/messages", "model": "claude-haiku-4-5", "raw_input_tokens": 1000, "yscompress_input_tokens": 800, "yellowstone_final_input_tokens": 800, "output_tokens": 50, "tokens_saved": 200, "compression_ratio": 0.8, "cache_mode": "auto"}, project_id=project_id, key_prefix=f"dc_{label[:6]}")
    return {"ccr_id": ccr["id"]}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProjectIsolation:
    """Storage-layer isolation: every query must filter by project_id."""

    def test_ccr_isolation(self, store, two_projects, crypto, scanner):
        proj_a, proj_b = two_projects
        seeds_a = _seed_project(store, crypto, scanner, proj_a["id"], "alpha")
        seeds_b = _seed_project(store, crypto, scanner, proj_b["id"], "beta")

        # project A cannot read project B's CCR
        assert store.get_ccr(project_id=proj_a["id"], ccr_id=seeds_b["ccr_id"], crypto=crypto) is None
        # project B cannot read project A's CCR
        assert store.get_ccr(project_id=proj_b["id"], ccr_id=seeds_a["ccr_id"], crypto=crypto) is None
        # each project can read its own CCR
        assert store.get_ccr(project_id=proj_a["id"], ccr_id=seeds_a["ccr_id"], crypto=crypto) is not None
        assert store.get_ccr(project_id=proj_b["id"], ccr_id=seeds_b["ccr_id"], crypto=crypto) is not None

    def test_devices_isolation(self, store, two_projects, crypto, scanner):
        proj_a, proj_b = two_projects
        _seed_project(store, crypto, scanner, proj_a["id"], "alpha")
        _seed_project(store, crypto, scanner, proj_b["id"], "beta")

        devices_a = store.list_devices(project_id=proj_a["id"])
        devices_b = store.list_devices(project_id=proj_b["id"])
        ids_a = {d["id"] for d in devices_a}
        ids_b = {d["id"] for d in devices_b}

        assert ids_a == {"dev-alpha"}
        assert ids_b == {"dev-beta"}
        assert ids_a.isdisjoint(ids_b), "Device sets must not overlap"

    def test_deployments_isolation(self, store, two_projects, crypto, scanner):
        proj_a, proj_b = two_projects
        _seed_project(store, crypto, scanner, proj_a["id"], "alpha")
        _seed_project(store, crypto, scanner, proj_b["id"], "beta")

        deps_a = store.list_deployments(project_id=proj_a["id"])
        deps_b = store.list_deployments(project_id=proj_b["id"])
        names_a = {d["name"] for d in deps_a}
        names_b = {d["name"] for d in deps_b}

        assert names_a == {"deploy-alpha"}
        assert names_b == {"deploy-beta"}
        assert names_a.isdisjoint(names_b), "Deployment sets must not overlap"

    def test_usage_isolation(self, store, two_projects, crypto, scanner):
        proj_a, proj_b = two_projects
        _seed_project(store, crypto, scanner, proj_a["id"], "alpha")
        _seed_project(store, crypto, scanner, proj_b["id"], "beta")

        summary_a = store.usage_summary(project_id=proj_a["id"])
        summary_b = store.usage_summary(project_id=proj_b["id"])

        # each project sees exactly 1 request — its own
        assert summary_a["requests"] == 1
        assert summary_b["requests"] == 1
        assert summary_a["raw_input_tokens"] == 1000
        assert summary_b["raw_input_tokens"] == 1000

        # export also scoped
        export_a = store.export_usage(project_id=proj_a["id"])
        export_b = store.export_usage(project_id=proj_b["id"])
        project_ids_in_a = {r["project_id"] for r in export_a}
        project_ids_in_b = {r["project_id"] for r in export_b}
        assert project_ids_in_a == {proj_a["id"]}
        assert project_ids_in_b == {proj_b["id"]}

    def test_api_keys_isolation(self, store, two_projects):
        proj_a, proj_b = two_projects
        key_a = store.create_api_key(project_id=proj_a["id"], name="key-a", scopes=["messages:write"])
        key_b = store.create_api_key(project_id=proj_b["id"], name="key-b", scopes=["messages:write"])

        # each project's key list only shows its own keys
        keys_a = store.list_api_keys(project_id=proj_a["id"])
        keys_b = store.list_api_keys(project_id=proj_b["id"])
        key_ids_a = {k["id"] for k in keys_a}
        key_ids_b = {k["id"] for k in keys_b}
        assert key_a["id"] in key_ids_a
        assert key_b["id"] not in key_ids_a
        assert key_b["id"] in key_ids_b
        assert key_a["id"] not in key_ids_b

        # key verification returns the correct project
        verified_a = store.verify_api_key(key_a["api_key"])
        verified_b = store.verify_api_key(key_b["api_key"])
        assert verified_a["project_id"] == proj_a["id"]
        assert verified_b["project_id"] == proj_b["id"]


class TestKeyRevocation:
    def test_revocation_prevents_use(self, store, two_projects):
        proj_a, _ = two_projects
        key = store.create_api_key(project_id=proj_a["id"], name="temp-key", scopes=["messages:write"])

        assert store.verify_api_key(key["api_key"]) is not None

        ok = store.revoke_api_key(project_id=proj_a["id"], key_id=key["id"], actor_prefix="dc_admin")
        assert ok is True

        # revoked key must not verify
        assert store.verify_api_key(key["api_key"]) is None

    def test_cannot_revoke_other_projects_key(self, store, two_projects):
        proj_a, proj_b = two_projects
        key_b = store.create_api_key(project_id=proj_b["id"], name="key-b", scopes=["messages:write"])

        # project A tries to revoke project B's key — returns False (not found in project A)
        ok = store.revoke_api_key(project_id=proj_a["id"], key_id=key_b["id"])
        assert ok is False

        # key B is still valid
        assert store.verify_api_key(key_b["api_key"]) is not None


class TestScopeEnforcement:
    def test_admin_scope_has_all(self):
        p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc_test", scopes=["admin"], auth_mode="api_key")
        # admin implicitly has every scope
        p.require("messages:write", "usage:read", "keys:admin", "ccr:read", "deployments:write")

    def test_narrow_scope_blocks_others(self):
        p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc_test", scopes=["messages:write"], auth_mode="api_key")
        # allowed
        p.require("messages:write")
        # blocked
        with pytest.raises(HTTPException) as exc_info:
            p.require("keys:admin")
        assert exc_info.value.status_code == 403
        assert "missing_scope" in str(exc_info.value.detail)

    def test_usage_read_scope(self):
        p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc_test", scopes=["usage:read"], auth_mode="api_key")
        p.require("usage:read")
        with pytest.raises(HTTPException):
            p.require("messages:write")

    def test_multi_scope_key(self):
        p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc_test", scopes=["messages:write", "usage:read", "ccr:read"], auth_mode="api_key")
        p.require("messages:write")
        p.require("usage:read")
        p.require("ccr:read")
        with pytest.raises(HTTPException):
            p.require("keys:admin")


class TestAuditLog:
    def test_key_creation_audited(self, store, two_projects):
        proj_a, _ = two_projects
        store.create_api_key(project_id=proj_a["id"], name="audited-key", scopes=["messages:write"])
        log = store.list_audit_log(project_id=proj_a["id"], action="api_key.create")
        assert len(log) >= 1
        assert log[0]["action"] == "api_key.create"
        assert log[0]["project_id"] == proj_a["id"]

    def test_key_revocation_audited(self, store, two_projects):
        proj_a, _ = two_projects
        key = store.create_api_key(project_id=proj_a["id"], name="revoke-me", scopes=["messages:write"])
        store.revoke_api_key(project_id=proj_a["id"], key_id=key["id"], actor_prefix="dc_adminx")
        log = store.list_audit_log(project_id=proj_a["id"], action="api_key.revoke")
        assert len(log) == 1
        assert log[0]["target_id"] == key["id"]
        assert log[0]["actor_key_prefix"] == "dc_adminx"

    def test_audit_log_scoped_to_project(self, store, two_projects):
        proj_a, proj_b = two_projects
        store.create_api_key(project_id=proj_a["id"], name="key-a", scopes=["messages:write"])
        store.create_api_key(project_id=proj_b["id"], name="key-b", scopes=["messages:write"])

        log_a = store.list_audit_log(project_id=proj_a["id"])
        log_b = store.list_audit_log(project_id=proj_b["id"])
        project_ids_in_a = {e["project_id"] for e in log_a}
        project_ids_in_b = {e["project_id"] for e in log_b}

        # audit entries from project A never appear in project B's log
        assert project_ids_in_a == {proj_a["id"]}
        assert project_ids_in_b == {proj_b["id"]}
        assert project_ids_in_a.isdisjoint(project_ids_in_b)

    def test_ccr_read_audit_written_separately(self, store, two_projects, crypto, scanner):
        proj_a, _ = two_projects
        ccr = store.put_ccr(project_id=proj_a["id"], original_text="sensitive data", metadata={}, crypto=crypto, scanner=scanner)
        store.record_audit(project_id=proj_a["id"], actor_key_prefix="dc_reader", action="ccr.read", target_type="ccr", target_id=ccr["id"])
        log = store.list_audit_log(project_id=proj_a["id"], action="ccr.read")
        assert len(log) == 1
        assert log[0]["target_id"] == ccr["id"]
        assert log[0]["target_type"] == "ccr"


class TestAdminBootstrap:
    def test_bootstrap_creates_admin_key(self, store):
        result = store.bootstrap_admin(email="founder@decompute.ai")
        assert result["api_key"].startswith("dc_")
        assert "admin" in result["scopes"]
        # key must verify immediately
        verified = store.verify_api_key(result["api_key"])
        assert verified is not None
        assert "admin" in verified["scopes"]

    def test_bootstrap_audited(self, store):
        result = store.bootstrap_admin(email="founder@decompute.ai")
        log = store.list_audit_log(project_id=result["project_id"], action="admin.bootstrap")
        assert len(log) == 1
        assert log[0]["target_id"] == result["api_key_id"]
