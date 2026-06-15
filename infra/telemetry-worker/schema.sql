-- Decompute telemetry — anonymous, COUNTS-ONLY savings rollups.
-- One row per (device, ISO-week). A re-send of the same week REPLACES the row (upsert in the Worker),
-- so a device that beacons twice in a week cannot double-count. No user content is ever stored here.

CREATE TABLE IF NOT EXISTS savings (
  device_hash      TEXT    NOT NULL,   -- sha256 of the client's random device UUID (never the raw id)
  week             TEXT    NOT NULL,   -- ISO week, e.g. "2026-W24"
  requests         INTEGER NOT NULL DEFAULT 0,
  tokens_saved     INTEGER NOT NULL DEFAULT 0,
  tokens_before    INTEGER NOT NULL DEFAULT 0,
  est_savings_usd  REAL    NOT NULL DEFAULT 0,
  os               TEXT,               -- coarse: "darwin" / "win32" / "linux" (no version/build)
  version          TEXT,               -- gateway version, for adoption tracking
  updated_at       INTEGER NOT NULL,   -- epoch ms of last write
  PRIMARY KEY (device_hash, week)
);

-- Fast weekly rollups for a dashboard ("savings this week" / "active devices this week").
CREATE INDEX IF NOT EXISTS idx_savings_week ON savings (week);
