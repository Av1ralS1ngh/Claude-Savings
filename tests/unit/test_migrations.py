"""
Migration tests.

Two scenarios:
  1. blank_db  — fresh database gets full schema via alembic upgrade head
  2. stale_dev — old hand-rolled SQLite schema (pre-V0-P0) is detected as
                 incompatible; recommended fix (drop + recreate) is verified

Alembic is run against SQLite so the tests work without a Postgres server.
The same migration file runs on Postgres in production — SQLAlchemy normalises
the DDL so this exercises the real migration code path.
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import QueuePool

from decompute_gateway.config import Settings
from decompute_gateway.db import Store

EXPECTED_TABLES = {
    "orgs", "users", "memberships", "projects",
    "api_keys", "devices", "deployments",
    "ccr_items", "usage_events", "audit_logs",
    "teacher_student_runs", "teacher_student_cases", "teacher_student_verdicts",
}

NEW_CCR_COLUMNS = {
    "encrypted_payload", "encryption_key_id", "storage_mode",
    "redacted_summary", "secret_scan_status", "expires_at",
}
NEW_USAGE_COLUMNS = {
    "yscompress_input_tokens", "yellowstone_final_input_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "estimated_cost_usd", "estimated_savings_usd",
    "billable_input_equivalent_tokens", "request_id", "upstream_request_id",
    "latency_ms", "status_code",
}


@contextmanager
def _db_url_env(db_path: Path):
    """Temporarily set DECOMPUTE_DATABASE_URL so alembic/env.py picks it up."""
    url = f"sqlite+pysqlite:///{db_path}"
    old = os.environ.get("DECOMPUTE_DATABASE_URL")
    os.environ["DECOMPUTE_DATABASE_URL"] = url
    try:
        yield url
    finally:
        if old is None:
            os.environ.pop("DECOMPUTE_DATABASE_URL", None)
        else:
            os.environ["DECOMPUTE_DATABASE_URL"] = old


def _run_alembic(db_path: Path, direction: str = "head") -> None:
    """Run alembic upgrade/downgrade with the test DB URL."""
    from alembic.config import Config
    from alembic import command
    with _db_url_env(db_path):
        cfg = Config("alembic.ini")
        if direction == "head":
            command.upgrade(cfg, "head")
        elif direction == "base":
            command.downgrade(cfg, "base")
        else:
            raise ValueError(direction)


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    conn.close()
    return cols


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    return names


def _indexes(db_path: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    insp = inspect(engine)
    return {i["name"] for i in insp.get_indexes(table)}


# ─── Scenario 1: blank DB ─────────────────────────────────────────────────────

class TestBlankDbMigration:
    """alembic upgrade head on a completely empty database."""

    def test_creates_all_tables(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        assert EXPECTED_TABLES.issubset(_table_names(db)), \
            f"Missing: {EXPECTED_TABLES - _table_names(db)}"

    def test_ccr_encryption_columns_present(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        missing = NEW_CCR_COLUMNS - _columns(db, "ccr_items")
        assert not missing, f"Missing CCR columns: {missing}"

    def test_usage_attribution_columns_present(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        missing = NEW_USAGE_COLUMNS - _columns(db, "usage_events")
        assert not missing, f"Missing usage columns: {missing}"

    def test_composite_indexes_created(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        assert "ix_usage_project_created" in _indexes(db, "usage_events")
        assert "ix_usage_project_model_created" in _indexes(db, "usage_events")
        assert "ix_ccr_project_created" in _indexes(db, "ccr_items")

    def test_alembic_version_stamped(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "alembic_version" in tables
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        conn.close()
        # Assert it matches the current Alembic head (robust to new migrations).
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        assert version is not None and version[0] == head

    def test_downgrade_removes_all_tables(self, tmp_path):
        db = tmp_path / "blank.db"
        _run_alembic(db)
        _run_alembic(db, direction="base")
        remaining = _table_names(db) & EXPECTED_TABLES
        assert not remaining, f"Tables still present after downgrade: {remaining}"

    def test_upgrade_is_idempotent(self, tmp_path):
        """Running upgrade head twice on the same DB must not raise."""
        db = tmp_path / "blank.db"
        _run_alembic(db)
        # second run should be a no-op
        _run_alembic(db)
        assert EXPECTED_TABLES.issubset(_table_names(db))

    def test_create_schema_for_local_dev_equivalent(self, tmp_path):
        """SQLAlchemy create_all path (used in local dev) produces the same tables."""
        settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/dev.db")
        Store(settings).create_schema_for_local_dev()
        assert EXPECTED_TABLES.issubset(_table_names(tmp_path / "dev.db"))

    def test_pool_config_applied_for_postgres_url(self):
        """pool_size + max_overflow are wired into the engine for non-SQLite URLs."""
        pytest.importorskip("psycopg", reason="psycopg not installed — skipping Postgres pool test")
        settings = Settings(
            database_url="postgresql+psycopg://fake:fake@localhost/fake",
            db_pool_size=3,
            db_max_overflow=7,
        )
        store = Store(settings)
        pool = store.engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.size() == 3
        assert pool._max_overflow == 7


# ─── Scenario 2: stale dev DB ─────────────────────────────────────────────────

OLD_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL,
  key_hash TEXT NOT NULL, key_prefix TEXT NOT NULL,
  created_at REAL NOT NULL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id TEXT PRIMARY KEY, project_id TEXT, endpoint TEXT NOT NULL, model TEXT,
  raw_input_tokens INTEGER NOT NULL, compressed_input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL, tokens_saved INTEGER NOT NULL,
  compression_ratio REAL NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ccr_items (
  id TEXT PRIMARY KEY, project_id TEXT, sha256 TEXT NOT NULL,
  original_text TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def _make_stale_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA_SQL)
    conn.execute("INSERT INTO projects VALUES ('proj_default','Default',1717000000)")
    conn.commit()
    conn.close()


class TestStaleSqliteSchema:
    """Old hand-rolled dev SQLite (pre-V0-P0) is detected and handled."""

    def test_stale_db_missing_new_tables(self, tmp_path):
        db = tmp_path / "stale.db"
        _make_stale_db(db)
        tables = _table_names(db)
        assert "orgs" not in tables
        assert "audit_logs" not in tables

    def test_stale_db_missing_encryption_columns(self, tmp_path):
        db = tmp_path / "stale.db"
        _make_stale_db(db)
        assert "encrypted_payload" not in _columns(db, "ccr_items")

    def test_stale_db_missing_usage_attribution_columns(self, tmp_path):
        db = tmp_path / "stale.db"
        _make_stale_db(db)
        assert "yscompress_input_tokens" not in _columns(db, "usage_events")

    def test_stale_db_alembic_fails_table_already_exists(self, tmp_path):
        """alembic upgrade head on a stale DB raises: tables already exist."""
        db = tmp_path / "stale.db"
        _make_stale_db(db)
        with pytest.raises(Exception, match="already exists|OperationalError"):
            _run_alembic(db)

    def test_drop_recreate_produces_full_schema(self, tmp_path):
        """Recommended fix: delete stale DB, run create_schema_for_local_dev."""
        db = tmp_path / "stale.db"
        _make_stale_db(db)
        db.unlink()
        Store(Settings(database_url=f"sqlite+pysqlite:///{db}")).create_schema_for_local_dev()
        assert "encrypted_payload" in _columns(db, "ccr_items")
        assert "yscompress_input_tokens" in _columns(db, "usage_events")
        assert EXPECTED_TABLES.issubset(_table_names(db))

    def test_existing_data_survives_drop_recreate(self, tmp_path):
        """After recreating, the default project is seeded and new data can be written."""
        db = tmp_path / "stale.db"
        db.unlink() if db.exists() else None
        settings = Settings(database_url=f"sqlite+pysqlite:///{db}")
        store = Store(settings)
        store.create_schema_for_local_dev()
        proj = store.create_project("Migrated Project")
        assert proj["id"].startswith("proj_")
        assert store.get_project(proj["id"])["name"] == "Migrated Project"
