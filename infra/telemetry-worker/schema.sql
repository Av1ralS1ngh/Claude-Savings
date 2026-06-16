-- Decompute telemetry — anonymous, COUNTS-ONLY savings rollups.
-- One row per (device, ISO-week). A re-send of the same week REPLACES the row (upsert in the Worker),
-- so a device that beacons twice in a week cannot double-count. No user content is ever stored here.
--
-- Columns: device_hash = sha256 of the client's random device UUID (never the raw id);
--          week = ISO week e.g. "2026-W24"; os = coarse family "darwin"/"win32"/"linux" (no build);
--          version = gateway version; updated_at = epoch ms of last write.
--
-- NOTE: comments are kept on their OWN lines (no trailing `-- ...` after code) so this file still works
-- if a tool flattens it to a single line — an inline comment would otherwise swallow the rest.
CREATE TABLE IF NOT EXISTS savings (
  device_hash      TEXT    NOT NULL,
  week             TEXT    NOT NULL,
  requests         INTEGER NOT NULL DEFAULT 0,
  tokens_saved     INTEGER NOT NULL DEFAULT 0,
  tokens_before    INTEGER NOT NULL DEFAULT 0,
  est_savings_usd  REAL    NOT NULL DEFAULT 0,
  os               TEXT,
  version          TEXT,
  updated_at       INTEGER NOT NULL,
  PRIMARY KEY (device_hash, week)
);

-- Fast weekly rollups for a dashboard ("savings this week" / "active devices this week").
CREATE INDEX IF NOT EXISTS idx_savings_week ON savings (week);
