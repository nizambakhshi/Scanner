-- 0009_light_and_fast.sql — a smaller database that answers faster
--
-- Measured on the live (Supabase Mumbai) database on 2026-10-14: 299 MB total,
-- 93 % of it in three tables whose rows almost nobody reads —
--   candles_1d  140 MB / 927,509 rows / 6,160 sessions (back to 2002)
--   candles_1m  108 MB / 245,168 rows / 6 sessions
--   candles_1w   31 MB / 195,217 rows / 1,294 weeks (back to 2001)
-- — plus three indexes the planner never chooses, and the single slowest
-- statement in pg_stat_statements (a 6.9 s daily-sparkline read, see
-- app/lib/source-postgres.ts). This file is the database half of the fix; the
-- query half ships in the same commit.
--
-- Four changes, all of them reclaiming space the dashboard has proved it does
-- not use:
--
-- 1. candles_1d keeps the newest 500 sessions, candles_1w the newest 200 weeks.
--    Why those numbers: the daily/weekly tables exist for RSI-14, the 52-week
--    high/low and the /stocks trend glyph. Wilder RSI is seeded from the oldest
--    bar in the series and converges within ~60 bars ((13/14)^500 is ~1e-16), so
--    500 sessions is eight times the history that can still change a reading,
--    and 200 weeks is more than three times the 52-week window. The trimmed rows
--    are not lost — `scanner-sync-daily` can rebuild them from the broker at any
--    time; they simply are not what an intraday product reads.
--    This deletes ~870 k daily and ~150 k weekly rows, about 110 MB of the 299 MB.
--
-- 2. Three indexes dropped, ~22 MB and three per-insert update costs:
--      candles_1m_pkey          (id)   10 MB, 6 scans — a vestigial surrogate key;
--                                     the real key is (instrument_id,
--                                     candle_start_at), which is what every
--                                     reader and the upsert's ON CONFLICT uses.
--      candles_1d_date_idx      (trading_date) 8 MB, 8 scans — served the
--                                     sync's freshness probe; with 500 sessions
--                                     left, the primary key scan is cheaper than
--                                     maintaining a second index on a table that
--                                     is written in monthly batches.
--      candles_1m_complete_idx  (candle_start_at DESC) WHERE is_complete
--                              3.7 MB, 1 scan — every live reader filters by
--                              instrument first, and candles_1m now holds days,
--                              not years.
--    Keeping: candles_1m_instrument_minute_key (497,000 scans),
--    candles_1m_date_instrument_idx (6,114), candles_1d_pkey (947,090),
--    candles_1w_pkey (195,459).
--
-- 3. app_sessions: rows whose server-owned expiry has passed can no longer
--    authenticate anything (readToken rejects the cookie first), yet they were
--    never removed — 22 of 22 rows were expired, 16 of them revoked, and the
--    table had never been autovacuumed. Deleting them is a no-op for every
--    session that still works. The recurring cleanup lives in the login path
--    (app/lib/auth/session.ts), not here.
--
-- 4. ANALYZE so the plans built after the trim see the new row estimates.
--
-- Rollout notes:
-- * nothing here is destructive to data the product reads: candles_1m is
--   untouched, and both trimmed tables are reproducible from the broker;
-- * the trim runs BEFORE the drops so the cutoff probe can still use
--   candles_1d_date_idx;
-- * `DROP ... IF EXISTS` and the "delete below the cutoff" form make the whole
--   file safe to re-run after a mid-file failure (scanner-migrate resumes
--   statement by statement);
-- * freed space is not returned to the filesystem by a plain DELETE — run
--   `VACUUM (ANALYZE)` on the two tables afterwards from a direct session,
--   outside this transaction-less migration, or let autovacuum do it;
-- * 500/200 are literals on purpose: this is a one-time correction of what an
--   unbounded archive became, not a policy the worker should re-evaluate hourly
--   (the rolling policy is ops_settings['retention'], owned by the dashboard).

-- ── 1. daily + weekly history becomes a window, not an archive ──────────────

DELETE FROM candles_1d
WHERE trading_date < (
    SELECT trading_date
    FROM (SELECT DISTINCT trading_date FROM candles_1d ORDER BY trading_date DESC LIMIT 1 OFFSET 499) keep
);

DELETE FROM candles_1w
WHERE week_start < (
    SELECT week_start
    FROM (SELECT DISTINCT week_start FROM candles_1w ORDER BY week_start DESC LIMIT 1 OFFSET 199) keep
);

-- ── 2. indexes nothing reads ────────────────────────────────────────────────

-- Say explicitly which index identifies rows for logical decoding, so dropping
-- the surrogate primary key cannot leave candles_1m without a replica identity.
ALTER TABLE candles_1m REPLICA IDENTITY USING INDEX candles_1m_instrument_minute_key;

ALTER TABLE candles_1m DROP CONSTRAINT IF EXISTS candles_1m_pkey;
DROP INDEX IF EXISTS candles_1d_date_idx;
DROP INDEX IF EXISTS candles_1m_complete_idx;

-- ── 3. sessions that cannot authenticate anyone ─────────────────────────────

DELETE FROM app_sessions WHERE expires_at < now();

-- ── 4. planner statistics ───────────────────────────────────────────────────

ANALYZE candles_1m;
ANALYZE candles_1d;
ANALYZE candles_1w;
ANALYZE app_sessions;
