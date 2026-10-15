-- 0012_three_stage.sql — the three-stage discovery brief's persistence
--
-- ⚠️  DRAFT — NOT YET APPROVED FOR APPLICATION. ⚠️
-- This file is written, self-reviewed and chain-verified on a disposable database
-- (see rollout notes), and it must NOT be applied to the Mini or to Mumbai until
-- the operator has reviewed this exact file and approved the apply order (the
-- audit's Approval gate C). Nothing in the current builds writes to the two new
-- tables or raises the three new signal types; the only way this file does
-- anything at all is a deliberate `scanner-migrate` run against a real database.
--
-- Everything here is additive. No existing row, column, view or setting is
-- touched, and a worker/dashboard on the previous build keeps running unchanged.
--
-- Numbering note: the parked #91–99 series (stash 17ef66ea, branch
-- wip-91-99-parked) reserves a different "0012 parameter document" concept. If
-- that work ever lands, it renumbers; nothing here touches it.
--
-- Three changes, one per stage of the brief:
--
-- 1. daily_discovery_snapshots — Stage 1's persisted output (§4.5 of the brief:
--    "persist daily snapshots"). One row per (trading_date, instrument_id),
--    written by the pre-open discovery run (scanner-run-discovery, which lands
--    with an approved later phase) after scanner-sync-daily has refreshed
--    candles_1d; until then the table stays empty. The group is the brief's
--    four-way verdict — STRONG_BULLISH / EMERGING_CANDIDATE / NEUTRAL_WATCH /
--    EXCLUDED — and every vocabulary in the CHECKs mirrors, one for one, the
--    constants the analytics module already defines (worker/analytics/daily.py:
--    TREND_*, GROUP_*, CLASS_*). `reason` carries the basis line for a member
--    and the exclusion reason for an excluded row; `indicators` is the full
--    DailyReading fold as jsonb, so a past day can be reconstructed without
--    re-reading candles, and `completeness` is the fraction of those readings
--    that were actually available (never a fabricated zero, §4.5).
--
--    No entry-signal semantics live here: Stage 1 classifies and persists, it
--    never alerts — that rule of the brief is enforced in code, and this table
--    simply has no alert-shaped columns. Retention deliberately does not purge
--    it (the purge job targets the two hot 1-minute tables), the same
--    exemption candles_1d carries: roughly fifty thousand small rows a year is
--    the Stage 1 history the brief asks to keep.
--
-- 2. setup_candidates — the Stage 2/3 candidate record: the ORB-retest and
--    VWAP-reclaim shapes the live tracker finds, with the anti-chase class, the
--    readiness state and the score breakdown attached (§6/§8: the persistence
--    needed to reconstruct episodes and scores). PROVISIONAL SHAPE: nothing
--    writes this table yet — the observation-only wiring logs candidates
--    instead — and the writer arrives only with an approved later phase. Its
--    column vocabulary mirrors the constants in extension.py (NORMAL /
--    EXTENDED / BLOCKED / PENDING) and readiness.py (the seven states). If the
--    shape changes before the first writer lands, edit this unapplied draft;
--    there is no data to migrate.
--
--    `score_label` is deliberately unrestricted TEXT: the brief makes the
--    80/65/50 label bands configurable, so pinning the label words in a CHECK
--    would turn a knob into a migration.
--
-- 3. signals.signal_type gains the three provisional Stage 3 alert types:
--    SETUP_ENTRY_READY, SETUP_INVALIDATED, ENTRY_BLOCKED_EXTENDED. The CHECK
--    from 0011 is dropped and re-created with the full list — a row written
--    under the old list still validates under the new one, so the widening is
--    invisible to every existing writer. Nothing raises these types yet; they
--    become reachable only with the Phase D alerting change, and an unused type
--    that turns out to be misnamed is renamed by editing this draft.
--
--    ops_settings is NOT touched for any of this. The anti-chase limits, the
--    stage-2 gate and the new alert types all arrive as code-supplied defaults
--    for absent keys (the 0011 precedent), and the first save from the operator
--    writes them explicitly. Rewriting a saved document from a migration
--    remains the thing this series does not do.
--
-- Rollout notes, stated against the runner the operator actually uses:
-- * `scanner-migrate` strips the BEGIN/COMMIT below and autocommits one
--   statement per round trip, so this file is NOT atomic there; the statements
--   are ordered so that every intermediate state is one the running system can
--   keep working in (new tables are invisible to old code, and the CHECK
--   widening only ever loosens).
-- * Every statement is re-runnable: CREATE TABLE IF NOT EXISTS, CREATE INDEX
--   IF NOT EXISTS, DROP CONSTRAINT IF EXISTS before ADD CONSTRAINT. If a run
--   dies halfway, re-run the file — the only statement a partial run can leave
--   behind is a "constraint already exists" on the signals CHECK, recovered
--   with a hand-run DROP CONSTRAINT of that one name.
-- * Upgrade path verified on a disposable postgres:17 both ways — the full
--   0001..0012 chain from empty, and 0011 → 0012 alone on top of a database
--   that had applied everything before it. The 0011→0012 step adds two
--   (initially empty) tables and widens one CHECK; there is no data rewrite.
-- * Rollback (deliberately narrow, per the audit): drop the two tables and
--   restore the 0011 CHECK list. The restored list is strictly narrower, so it
--   is legal only while no row carries the three new types — true whenever the
--   Phase D alerting code has not shipped, and stated here so the order
--   (alerts off, then rollback) is explicit.
-- * scanner-migrate records the version row itself, so this file must not.

BEGIN;

-- 1 ── Stage 1: the daily discovery fold, persisted.

CREATE TABLE IF NOT EXISTS daily_discovery_snapshots (
    trading_date        DATE NOT NULL,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    discovery_group     TEXT NOT NULL CHECK (discovery_group IN ('STRONG_BULLISH',
                                                                 'EMERGING_CANDIDATE',
                                                                 'NEUTRAL_WATCH',
                                                                 'EXCLUDED')),
    trend_class         TEXT NOT NULL CHECK (trend_class IN ('STRONG_BULL',
                                                             'BULL',
                                                             'EMERGING',
                                                             'NEUTRAL',
                                                             'WEAK',
                                                             'UNKNOWN')),
    liquidity_class     TEXT NOT NULL CHECK (liquidity_class IN ('HIGH',
                                                                 'MEDIUM',
                                                                 'LOW',
                                                                 'INSUFFICIENT')),
    volatility_class    TEXT NOT NULL CHECK (volatility_class IN ('HIGH',
                                                                  'MEDIUM',
                                                                  'LOW',
                                                                  'INSUFFICIENT')),
    relative_strength   NUMERIC(8,4),
    eligibility         BOOLEAN NOT NULL,
    reason              TEXT NOT NULL,
    indicators          JSONB NOT NULL DEFAULT '{}'::JSONB,
    completeness        NUMERIC(5,4) NOT NULL CHECK (completeness >= 0 AND completeness <= 1),
    universe            TEXT NOT NULL DEFAULT 'FNO',
    config_version      INTEGER NOT NULL DEFAULT 1,
    is_simulated        BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: one row per day per instrument. A re-run of the discovery
    -- (restart, retry, or the operator re-invoking it) upserts, never duplicates.
    PRIMARY KEY (trading_date, instrument_id)
);
CREATE INDEX IF NOT EXISTS daily_discovery_snapshots_group_idx
    ON daily_discovery_snapshots (trading_date, discovery_group);
CREATE INDEX IF NOT EXISTS daily_discovery_snapshots_instrument_idx
    ON daily_discovery_snapshots (instrument_id, trading_date DESC);

-- 2 ── Stage 2/3: the candidate record (provisional shape — see header).

CREATE TABLE IF NOT EXISTS setup_candidates (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trading_session_id  BIGINT REFERENCES trading_sessions(id) ON DELETE SET NULL,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    candle_start_at     TIMESTAMPTZ NOT NULL,
    setup               TEXT NOT NULL CHECK (setup IN ('orb_retest','vwap_reclaim')),
    level_price         NUMERIC(16,4) NOT NULL,
    invalidation_price  NUMERIC(16,4),
    extension_class     TEXT CHECK (extension_class IS NULL OR extension_class IN ('NORMAL',
                                                                                   'EXTENDED',
                                                                                   'BLOCKED',
                                                                                   'PENDING')),
    readiness_state     TEXT CHECK (readiness_state IS NULL OR readiness_state IN ('WATCH',
                                                                                   'SETUP_DEVELOPING',
                                                                                   'ENTRY_READY',
                                                                                   'EXTENDED_WAIT',
                                                                                   'WEAKENING',
                                                                                   'INVALIDATED',
                                                                                   'STALE')),
    score_total         NUMERIC(6,2),
    score_label         TEXT,
    score_completeness  NUMERIC(5,4) CHECK (score_completeness IS NULL
                                            OR (score_completeness >= 0 AND score_completeness <= 1)),
    score_breakdown     JSONB NOT NULL DEFAULT '{}'::JSONB,
    reasons             TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    idempotency_key     TEXT NOT NULL,
    config_version      INTEGER NOT NULL DEFAULT 1,
    is_simulated        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One row per candidate episode, the same identity discipline signals use:
    -- a restart that re-derives the same candidate writes nothing new.
    CONSTRAINT setup_candidates_idempotency_key_key UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS setup_candidates_session_idx
    ON setup_candidates (trading_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS setup_candidates_instrument_idx
    ON setup_candidates (instrument_id, candle_start_at DESC);

-- 3 ── the three provisional Stage 3 alert types (widening only).

ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_signal_type_check;
ALTER TABLE signals ADD CONSTRAINT signals_signal_type_check CHECK (
    signal_type IN ('BULLISH_ENTRY',
                    'WATCHLIST_ADDED',
                    'WATCHLIST_WEAKENING',
                    'WATCHLIST_REMOVED',
                    'WATCHLIST_REQUALIFIED',
                    'DATA_FEED_INTERRUPTED',
                    'DATA_FEED_RECOVERED',
                    'DAILY_SCAN_COMPLETE',
                    'RSI_BULLISH',
                    'RSI_BEARISH',
                    'RSI_ENTRY',
                    'RSI_OVERBOUGHT',
                    'BREAKOUT_UP',
                    'BREAKDOWN',
                    'SETUP_ENTRY_READY',
                    'SETUP_INVALIDATED',
                    'ENTRY_BLOCKED_EXTENDED')
);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['daily_discovery_snapshots', 'setup_candidates']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
    END LOOP;
END
$$;

COMMIT;
