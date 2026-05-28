from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  name TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  created_at REAL NOT NULL,
  revoked_at REAL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  profile_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  name TEXT NOT NULL UNIQUE,
  plan_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ccr_items (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  sha256 TEXT NOT NULL,
  original_text TEXT NOT NULL,
  metadata_json TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  api_key_prefix TEXT,
  endpoint TEXT NOT NULL,
  model TEXT,
  raw_input_tokens INTEGER NOT NULL,
  compressed_input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  tokens_saved INTEGER NOT NULL,
  compression_ratio REAL NOT NULL,
  cache_mode TEXT,
  route_json TEXT,
  created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        self.ensure_default_project()

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:16]}"

    @staticmethod
    def hash_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def ensure_default_project(self) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM projects WHERE id='proj_default'").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
                    ("proj_default", "Default Project", self._now()),
                )
                conn.commit()
        return "proj_default"

    def create_project(self, name: str) -> dict[str, Any]:
        project = {"id": self._id("proj"), "name": name, "created_at": self._now()}
        with self.connect() as conn:
            conn.execute("INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", tuple(project.values()))
            conn.commit()
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created_at DESC")]

    def create_api_key(self, name: str, project_id: str = "proj_default") -> dict[str, Any]:
        raw = "dc_" + secrets.token_urlsafe(32)
        key = {
            "id": self._id("key"),
            "project_id": project_id,
            "name": name,
            "key_hash": self.hash_key(raw),
            "key_prefix": raw[:10],
            "created_at": self._now(),
            "revoked_at": None,
        }
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO api_keys(id, project_id, name, key_hash, key_prefix, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key["id"], key["project_id"], key["name"], key["key_hash"], key["key_prefix"], key["created_at"], None),
            )
            conn.commit()
        public = {k: v for k, v in key.items() if k != "key_hash"}
        public["api_key"] = raw
        return public

    def list_api_keys(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, project_id, name, key_prefix, created_at, revoked_at FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def verify_api_key(self, raw_key: str) -> dict[str, Any] | None:
        if not raw_key:
            return None
        key_hash = self.hash_key(raw_key)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, project_id, name, key_prefix, created_at FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
                (key_hash,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_device(self, profile: dict[str, Any], project_id: str = "proj_default") -> dict[str, Any]:
        now = self._now()
        profile = dict(profile)
        device_id = profile.get("id") or profile.get("device_id") or self._id("dev")
        profile["id"] = device_id
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO devices(id, project_id, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at
                """,
                (device_id, project_id, json.dumps(profile), now, now),
            )
            conn.commit()
        return profile

    def list_devices(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT profile_json FROM devices ORDER BY updated_at DESC").fetchall()
            return [json.loads(r["profile_json"]) for r in rows]

    def create_deployment(self, name: str, plan: dict[str, Any], project_id: str = "proj_default") -> dict[str, Any]:
        deployment = {"id": self._id("dep"), "project_id": project_id, "name": name, "plan": plan, "created_at": self._now()}
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deployments(id, project_id, name, plan_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (deployment["id"], project_id, name, json.dumps(plan), deployment["created_at"]),
            )
            conn.commit()
        return deployment

    def get_deployment_by_name(self, name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deployments WHERE name=?", (name,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["plan"] = json.loads(d.pop("plan_json"))
            return d

    def list_deployments(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM deployments ORDER BY created_at DESC").fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d["plan"] = json.loads(d.pop("plan_json"))
                out.append(d)
            return out

    def put_ccr(self, original_text: str, metadata: dict[str, Any] | None = None, project_id: str = "proj_default") -> str:
        digest = hashlib.sha256(original_text.encode("utf-8", errors="ignore")).hexdigest()
        ccr_id = "ccr_" + digest[:24]
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ccr_items(id, project_id, sha256, original_text, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ccr_id, project_id, digest, original_text, json.dumps(metadata or {}), self._now()),
            )
            conn.commit()
        return ccr_id

    def get_ccr(self, ccr_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ccr_items WHERE id=?", (ccr_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            return d

    def record_usage(self, event: dict[str, Any], project_id: str = "proj_default", key_prefix: str | None = None) -> dict[str, Any]:
        row = {
            "id": self._id("use"),
            "project_id": project_id,
            "api_key_prefix": key_prefix,
            "endpoint": event.get("endpoint", "unknown"),
            "model": event.get("model"),
            "raw_input_tokens": int(event.get("raw_input_tokens", 0)),
            "compressed_input_tokens": int(event.get("compressed_input_tokens", 0)),
            "output_tokens": int(event.get("output_tokens", 0)),
            "tokens_saved": int(event.get("tokens_saved", 0)),
            "compression_ratio": float(event.get("compression_ratio", 1.0)),
            "cache_mode": event.get("cache_mode"),
            "route_json": json.dumps(event.get("route") or {}),
            "created_at": self._now(),
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events(id, project_id, api_key_prefix, endpoint, model, raw_input_tokens,
                compressed_input_tokens, output_tokens, tokens_saved, compression_ratio, cache_mode, route_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row.values()),
            )
            conn.commit()
        return row

    def usage_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) requests,
                       COALESCE(SUM(raw_input_tokens),0) raw_input_tokens,
                       COALESCE(SUM(compressed_input_tokens),0) compressed_input_tokens,
                       COALESCE(SUM(output_tokens),0) output_tokens,
                       COALESCE(SUM(tokens_saved),0) tokens_saved
                FROM usage_events
                """
            ).fetchone()
            by_model = conn.execute(
                """
                SELECT model, COUNT(*) requests, COALESCE(SUM(tokens_saved),0) tokens_saved
                FROM usage_events GROUP BY model ORDER BY requests DESC
                """
            ).fetchall()
        summary = dict(row)
        raw = summary["raw_input_tokens"] or 0
        sent = summary["compressed_input_tokens"] or 0
        summary["compression_ratio"] = (sent / raw) if raw else 1.0
        summary["estimated_savings_percent"] = (1.0 - sent / raw) * 100 if raw else 0.0
        summary["by_model"] = [dict(r) for r in by_model]
        return summary
