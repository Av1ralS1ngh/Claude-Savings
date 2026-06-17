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

-- ── ADDITIONS (existing `savings` table is untouched; all additive, IF NOT EXISTS) ──
-- Comments stay on their OWN lines (existing convention) so a tool that flattens the file
-- to one line can't have a trailing inline comment swallow the rest.

-- Anonymous install counts, fired once per install by scripts/install.sh (POST /v1/event).
-- One row per (device_hash, UTC day, ci); a same-day re-run UPSERTs the same row -> no inflation.
-- device_hash = sha256 of the SAME 'dev-<uuidhex>' id in ~/.decompute/device.id, so a download
-- JOINs to that device's savings on savings.device_hash = downloads.device_hash.
-- day  = server-computed UTC 'YYYY-MM-DD' (idempotency window; never the client clock).
-- week = server-computed ISO 'YYYY-Www' (aligns with savings.week; queries GROUP BY it).
-- ci   = 1 for CI / non-interactive installs, else 0 (in the PK so real vs CI are separable rows).
-- os   = coarse family only: 'darwin' / 'linux' / 'win32' / 'unknown'.
CREATE TABLE IF NOT EXISTS downloads (
  device_hash  TEXT    NOT NULL,
  day          TEXT    NOT NULL,
  week         TEXT    NOT NULL,
  ci           INTEGER NOT NULL DEFAULT 0,
  os           TEXT,
  version      TEXT,
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY (device_hash, day, ci)
);

CREATE INDEX IF NOT EXISTS idx_downloads_week ON downloads (week);
CREATE INDEX IF NOT EXISTS idx_downloads_day  ON downloads (day);

-- Marketing-site visit counts (POST /v1/visit, browser).
-- visitor_hash = sha256( hmac(long-lived secret, per-day EPHEMERAL salt | day) | ip | ua ); the ip/ua
--   inputs are DROPPED in-handler and the ephemeral salt AUTO-EXPIRES, so stored hashes are not
--   brute-forceable back to (ip,ua) once the day passes (forward secrecy). There is NO ip/ua/url column.
-- day/week = server-computed UTC date / ISO week.
-- path  = allowlisted coarse pathname ('/', '/pro', ... or '/other'); never a full URL/query/free text.
-- nonce = sha256 of a per-pageload client nonce (the PLAINTEXT is never stored); dedupes double-beacons.
CREATE TABLE IF NOT EXISTS visits (
  visitor_hash  TEXT    NOT NULL,
  day           TEXT    NOT NULL,
  week          TEXT    NOT NULL,
  path          TEXT    NOT NULL,
  nonce         TEXT    NOT NULL,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (visitor_hash, day, path, nonce)
);

CREATE INDEX IF NOT EXISTS idx_visits_week ON visits (week);
CREATE INDEX IF NOT EXISTS idx_visits_day  ON visits (day, path);
