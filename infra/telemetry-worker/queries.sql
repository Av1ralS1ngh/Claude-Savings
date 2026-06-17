-- ============================================================================
-- Decompute telemetry — COMPANY-FACING QUERIES (run by hand / scheduled by us).
-- These are READ queries the company runs against the D1 database to build the
-- internal dashboard. They are NOT executed by the Worker request path.
--
--   wrangler d1 execute decompute-telemetry --remote --command "<one of these>"
--   wrangler d1 execute decompute-telemetry --remote --file queries.sql   # runs all
--
-- ----------------------------------------------------------------------------
-- LABEL SEMANTICS — read this before you put any of these numbers on a slide.
-- ----------------------------------------------------------------------------
-- We measure DEVICES and EVENTS, never PEOPLE. The schema is anonymous by
-- design (sha256 of a random client-side UUID; no IP, no UA, no email), so the
-- only honest unit is "a thing that beaconed". Use these exact words:
--
--   reporting_installs  = COUNT(DISTINCT device_hash) — distinct devices that
--                         have BEACONED (savings rollup and/or install ping).
--                         This is "installs that phoned home", NOT users/humans.
--                         A privacy-conscious user who set DO_NOT_TRACK never
--                         appears here at all.
--   downloads           = install EVENTS from the curl installer (scripts/install.sh),
--                         idempotent per (device_hash, day, ci) so re-running the
--                         installer the same day does NOT inflate. Split real vs CI.
--   visits              = DISTINCT cookieless visitor-hash per period from the
--                         marketing site. The visitor-hash rotates DAILY (salt is
--                         day-derived), so "distinct visitors" is only meaningful
--                         WITHIN a single day; across days the same browser hashes
--                         differently by design. Sum daily distincts for a weekly
--                         "visitor-days" figure — do not COUNT(DISTINCT) a hash
--                         across multiple days and call it "unique visitors".
--
-- ----------------------------------------------------------------------------
-- DIRECTIONAL CAVEATS — device_hash counting is approximate ON PURPOSE.
-- ----------------------------------------------------------------------------
-- Because device_hash = sha256(random per-machine UUID at ~/.decompute/device.id):
--
--   WIPE / REINSTALL  -> OVERCOUNT. Reinstalling the OS, deleting ~/.decompute,
--                        a fresh CI runner each job, or running in a container
--                        mints a NEW device.id -> a NEW device_hash -> counted as
--                        a new "reporting_install" even though it's the same human.
--                        (CI is the worst offender; this is exactly why we split
--                        ci=1 out below.)
--   CLONE / IMAGE     -> UNDERCOUNT. A VM/laptop golden image baked AFTER
--                        ~/.decompute/device.id existed copies that id to many
--                        machines -> they all share ONE device_hash -> counted as
--                        a single install. Shared/multi-user boxes also undercount.
--   DNT / opt-out     -> UNDERCOUNT. Users who disable telemetry never beacon, so
--                        they are invisible to every number here.
--
-- Net: treat reporting_installs as a DIRECTIONAL trend, not a headcount. Never
-- divide revenue by it to get ARPU, never call it MAU/DAU, never say "users".
-- The right framing for the boss: "devices that reported in", with the CI split
-- shown so the real-vs-automation mix is explicit.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- (1) WEEKLY TOKEN SAVINGS  (already-collected data; table: savings)
--     One row per ISO week. reporting_installs here = devices that sent a
--     savings rollup that week (a subset of all installs — only active devices).
-- ----------------------------------------------------------------------------
SELECT
  week,
  COUNT(DISTINCT device_hash)         AS reporting_installs,   -- active devices, NOT users
  COALESCE(SUM(requests), 0)          AS requests,
  COALESCE(SUM(tokens_saved), 0)      AS tokens_saved,
  COALESCE(SUM(tokens_before), 0)     AS tokens_before,
  ROUND(COALESCE(SUM(est_savings_usd), 0), 2) AS est_savings_usd
FROM savings
GROUP BY week
ORDER BY week DESC;


-- ----------------------------------------------------------------------------
-- (2) WEEKLY DOWNLOADS, split REAL vs CI  (table: downloads)
--     Schema assumed (created by the downloads endpoint in schema.sql):
--       downloads(device_hash TEXT, day TEXT 'YYYY-MM-DD', week TEXT 'YYYY-Www',
--                 ci INTEGER 0|1, os TEXT, updated_at INTEGER,
--                 PRIMARY KEY (device_hash, day, ci))
--     Idempotency: PK (device_hash, day, ci) + INSERT OR IGNORE means re-running
--     the installer the same day is a no-op -> one install event per device/day/ci.
--     We count install EVENTS (rows). ci is kept SEPARATE so CI runners (which
--     mint a fresh device.id every job) can't masquerade as real adoption.
-- ----------------------------------------------------------------------------
SELECT
  week,
  COUNT(*)                                              AS download_events,        -- all install pings
  SUM(CASE WHEN ci = 0 THEN 1 ELSE 0 END)              AS real_downloads,         -- interactive, real machines
  SUM(CASE WHEN ci = 1 THEN 1 ELSE 0 END)              AS ci_downloads,           -- CI / non-tty
  COUNT(DISTINCT CASE WHEN ci = 0 THEN device_hash END) AS real_reporting_installs -- distinct real devices
FROM downloads
GROUP BY week
ORDER BY week DESC;


-- ----------------------------------------------------------------------------
-- (3) WEEKLY DISTINCT VISITORS  (table: visits)
--     Schema assumed (created by the visit endpoint in schema.sql):
--       visits(visitor_hash TEXT, day TEXT 'YYYY-MM-DD', week TEXT 'YYYY-Www',
--              path TEXT, nonce TEXT, updated_at INTEGER,
--              PRIMARY KEY (visitor_hash, day, path, nonce))
--     visitor_hash is cookieless and rotates DAILY (salt = day-derived), so a
--     visitor is only comparable WITHIN a day. We therefore compute DAILY
--     distinct visitor-hashes and SUM them into a weekly "visitor-days" total —
--     NOT a cross-day COUNT(DISTINCT), which would be meaningless (same browser
--     hashes differently each day on purpose).
--     The nonce is per-PAGELOAD, so it collapses the sendBeacon/fetch double-fire and instant
--     retries WITHIN one load — it does NOT collapse genuine reloads (an F5 storm = new nonces =
--     new rows). Treat visitor_days (distinct visitor_hash/day) as the trustworthy metric and
--     pageviews as INFLATABLE / directional. nonce is stored as sha256(nonce), never plaintext.
-- ----------------------------------------------------------------------------
SELECT
  week,
  SUM(daily_distinct_visitors) AS visitor_days,   -- sum of per-day distinct visitor-hashes
  SUM(daily_pageviews)         AS pageviews
FROM (
  SELECT
    week,
    day,
    COUNT(DISTINCT visitor_hash) AS daily_distinct_visitors,
    COUNT(*)                     AS daily_pageviews
  FROM visits
  GROUP BY week, day
)
GROUP BY week
ORDER BY week DESC;


-- ----------------------------------------------------------------------------
-- (4) COMBINED WEEKLY DASHBOARD ROW
--     One row per week stitching savings + downloads + visits with a FULL OUTER
--     JOIN emulation (SQLite has no FULL JOIN) over the union of all weeks, so a
--     week that has visits but no savings (pre-launch traffic) still shows up.
--     Every metric carries its honest label; reporting_installs is "devices that
--     reported in that week", explicitly NOT users.
-- ----------------------------------------------------------------------------
WITH weeks AS (
  SELECT week FROM savings
  UNION
  SELECT week FROM downloads
  UNION
  SELECT week FROM visits
),
sav AS (
  SELECT week,
         COUNT(DISTINCT device_hash)         AS active_reporting_installs,
         COALESCE(SUM(tokens_saved), 0)      AS tokens_saved,
         ROUND(COALESCE(SUM(est_savings_usd), 0), 2) AS est_savings_usd
  FROM savings GROUP BY week
),
dl AS (
  SELECT week,
         SUM(CASE WHEN ci = 0 THEN 1 ELSE 0 END) AS real_downloads,
         SUM(CASE WHEN ci = 1 THEN 1 ELSE 0 END) AS ci_downloads
  FROM downloads GROUP BY week
),
vis AS (
  SELECT week, SUM(daily_distinct_visitors) AS visitor_days FROM (
    SELECT week, day, COUNT(DISTINCT visitor_hash) AS daily_distinct_visitors
    FROM visits GROUP BY week, day
  ) GROUP BY week
)
SELECT
  w.week                                       AS week,
  COALESCE(vis.visitor_days, 0)                AS visitor_days,         -- marketing-site distinct visitors/day, summed
  COALESCE(dl.real_downloads, 0)               AS real_downloads,       -- installer pings, CI excluded
  COALESCE(dl.ci_downloads, 0)                 AS ci_downloads,         -- CI / non-tty installs (separate)
  COALESCE(sav.active_reporting_installs, 0)   AS active_reporting_installs, -- devices that beaconed savings (NOT users)
  COALESCE(sav.tokens_saved, 0)                AS tokens_saved,
  COALESCE(sav.est_savings_usd, 0)             AS est_savings_usd
FROM weeks w
LEFT JOIN sav ON sav.week = w.week
LEFT JOIN dl  ON dl.week  = w.week
LEFT JOIN vis ON vis.week = w.week
ORDER BY w.week DESC;


-- ----------------------------------------------------------------------------
-- (5) ALL-TIME reporting_installs across BOTH beacon sources (savings + downloads)
--     The most honest "total installs that phoned home" number: distinct
--     device_hash seen in EITHER table. Still subject to the wipe/clone/opt-out
--     caveats above — directional, not a headcount.
-- CAVEAT: the ci split exists ONLY for downloads; `savings` has no ci dimension, so a gateway that
-- ran in CI and sent a savings rollup is counted here as a real reporting_install. This number is
-- directional (see the wipe/clone/opt-out caveats above) — never present it as users/MAU/DAU.
-- ----------------------------------------------------------------------------
SELECT COUNT(DISTINCT device_hash) AS reporting_installs_all_time
FROM (
  SELECT device_hash FROM savings
  UNION
  SELECT device_hash FROM downloads WHERE ci = 0
);


-- ----------------------------------------------------------------------------
-- (6) FUNNEL: real installs that LATER reported savings (engagement signal).
--     Higher-trust than downloads_total (forging this requires a real device.id that also
--     runs the gateway). LEFT JOIN so a download with no savings still shows.
-- ----------------------------------------------------------------------------
SELECT
  COUNT(DISTINCT d.device_hash)                                              AS real_installs,
  COUNT(DISTINCT CASE WHEN s.device_hash IS NOT NULL THEN d.device_hash END) AS installs_that_reported
FROM downloads d
LEFT JOIN savings s ON s.device_hash = d.device_hash
WHERE d.ci = 0;
