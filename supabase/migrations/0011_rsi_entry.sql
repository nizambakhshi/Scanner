-- 0011_rsi_entry.sql — RSI as an entry rule, and where its reading lives
--
-- The operator's rule (2026-10-15), replacing the 2026-10-12 momentum reading:
--
--   RSI below 40  -> green, it is an ENTRY
--   RSI above 60  -> red
--
-- and judged "at the time of a bullish or bearish trend", not on the nightly
-- daily/weekly pair alone. So the app now has two RSI facts, and this file gives
-- each of them a name that means the same thing in the database, on the page and
-- in a Telegram message:
--
--   ENTRY       the oversold side (< 40). Green. This is the one to act on.
--   OVERBOUGHT  the expensive side (> 60). Red.
--   NEUTRAL     the 40-60 band, or a reading without the trend it needs.
--
-- Under the old vocabulary those two sides were called BEARISH and BULLISH, which
-- described momentum and pointed the colours the other way. Renaming rather than
-- re-colouring is deliberate: a stored 'BULLISH' row that renders red and pages
-- "stay away" is a trap for the next person to read either.
--
-- Four changes, and the read model that carries them:
--
-- 1. indicator_snapshots gains rsi_5m and rsi_state. The worker computes Wilder
--    RSI-14 on today's 5-minute closes next to the VWAP and EMA pair it already
--    records every minute, so the number the dashboard colours and the number the
--    detector alerted on are the same stored value, read from the same row. It is
--    NULL for the first ~75 minutes of a session (14 buckets plus the seed) and for
--    every candle stored before this file ran.
--
--    Not a second copy of the tape: candles_1m stays the only price history, and
--    rsi_5m is a *derived reading* attached to the candle it was measured on, the
--    same way ema9 already is.
--
-- 2. stock_details.rsi_bias and .last_alert_bias take the new vocabulary, with the
--    existing rows translated in place: BULLISH (both timeframes above 60) becomes
--    OVERBOUGHT, BEARISH (both below 40) becomes ENTRY. Both columns are nullable
--    and NEUTRAL means the same thing it did. Order matters: the old CHECK has to
--    go before the UPDATE, or the rename violates the constraint it is replacing.
--
-- 3. signals.signal_type gains RSI_ENTRY and RSI_OVERBOUGHT. RSI_BULLISH and
--    RSI_BEARISH stay legal so history written before today still reads; nothing is
--    rewritten, and the signals board shows whichever code a row carries.
--
-- 4. ops_settings['alert_strategies'] is NOT touched. A stored document that
--    predates the rsi strategy needs no new key: parse_strategies() supplies the
--    shipped defaults for any strategy the document omits, so the 40/60 pair and
--    its cooldown arrive as the worker's own defaults, and the first save from
--    /alerts writes all four strategies explicitly. Rewriting an operator's saved
--    document from a migration is the thing this file must not do.
--
-- Rollout notes, stated against the runner the operator actually uses:
-- * `scanner-migrate` strips the BEGIN/COMMIT below and autocommits one statement
--   per round trip — that is how it survives a managed pooler severing a
--   multi-statement batch — so this file is NOT atomic here, and the transaction
--   wrapper only applies to a client that runs the file whole (psql). The statements
--   are ordered so that every intermediate state is one the pages and the worker can
--   keep running in: the columns are added nullable, the old CHECK is dropped before
--   the UPDATE, and both UPDATEs are re-runnable because their WHERE clauses match
--   nothing once the translation has happened.
-- * If a statement does fail, everything before it is already committed and the file
--   is not registered, so the next run re-sends all of it. `ADD COLUMN IF NOT EXISTS`
--   and `DROP CONSTRAINT IF EXISTS` tolerate that; a plain `ADD CONSTRAINT` does not
--   ("constraint already exists"), and the recovery is a hand-run DROP CONSTRAINT for
--   that one name — or run the remaining statements directly — then re-run the file.
-- * The two new columns are nullable, so a worker on the previous build keeps writing
--   valid snapshot rows, and the dashboard may deploy before the worker restarts.
-- * The reverse order is not safe: the new build *selects* `rsi_5m` and `rsi_state`
--   from `v_current_watchlist` and from `indicator_snapshots`, so this file has to be
--   applied before that build ships. A page whose read fails shows an error panel —
--   here it would show one for the whole board.
-- * The other half of the ordering is the vocabulary: once the last statement of step
--   2 runs, `stock_details.rsi_bias` may only hold ENTRY/OVERBOUGHT/NEUTRAL, and the
--   previous worker build writes BULLISH/BEARISH there (the nightly sync, and
--   `set_last_alert_bias` during a session). So this file and the worker restart
--   belong to the same window, outside market hours and before the next 09:15. The
--   page in between is only cosmetic: the deployed board reads the translated words
--   with a matcher that no longer knows them, so every RSI cell shows a dash.
-- * `v_current_watchlist` can only be replaced with the existing columns in the same
--   positions and new ones appended; verified by running the full 0001..0011 chain
--   against a clean postgres:17.
-- * scanner-migrate records the version row itself, so this file must not.

BEGIN;

-- 1 ── the intraday reading, on the candle it was measured on.
ALTER TABLE indicator_snapshots ADD COLUMN IF NOT EXISTS rsi_5m NUMERIC(8,4);
ALTER TABLE indicator_snapshots ADD COLUMN IF NOT EXISTS rsi_state TEXT;
ALTER TABLE indicator_snapshots DROP CONSTRAINT IF EXISTS indicator_snapshots_rsi_state_check;
ALTER TABLE indicator_snapshots ADD CONSTRAINT indicator_snapshots_rsi_state_check CHECK (
    rsi_state IS NULL OR rsi_state IN ('ENTRY','OVERBOUGHT','NEUTRAL')
);

-- 2 ── the nightly pair, renamed. Drop first, translate, then constrain.
ALTER TABLE stock_details DROP CONSTRAINT IF EXISTS stock_details_rsi_bias_check;
ALTER TABLE stock_details DROP CONSTRAINT IF EXISTS stock_details_last_alert_bias_check;

UPDATE stock_details
   SET rsi_bias = CASE rsi_bias WHEN 'BULLISH' THEN 'OVERBOUGHT'
                               WHEN 'BEARISH'  THEN 'ENTRY'
                               ELSE rsi_bias END
 WHERE rsi_bias IN ('BULLISH','BEARISH');

UPDATE stock_details
   SET last_alert_bias = CASE last_alert_bias WHEN 'BULLISH' THEN 'OVERBOUGHT'
                                             WHEN 'BEARISH'  THEN 'ENTRY'
                                             ELSE last_alert_bias END
 WHERE last_alert_bias IN ('BULLISH','BEARISH');

ALTER TABLE stock_details ADD CONSTRAINT stock_details_rsi_bias_check CHECK (
    rsi_bias IN ('ENTRY','OVERBOUGHT','NEUTRAL')
);
ALTER TABLE stock_details ADD CONSTRAINT stock_details_last_alert_bias_check CHECK (
    last_alert_bias IN ('ENTRY','OVERBOUGHT','NEUTRAL')
);

-- 3 ── the alert types the two sides now raise.
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
                    'BREAKDOWN')
);

-- 4 ── the board's read model has to carry the reading, or the page cannot show it.
--
-- `v_current_watchlist` already reads the watchlist's newest snapshot for the VWAP,
-- EMA pair and relative volume it prints, so the five-minute RSI belongs in the same
-- place rather than in a second join per row. The list is the one from 0003_views.sql
-- with the two new columns added *at the end*, because `CREATE OR REPLACE VIEW` may
-- only append columns: it matches the existing view's columns positionally, so
-- inserting `rsi_5m` before `bullish_qualified` fails with
-- "cannot change name of view column", and a shorter list would silently delete a
-- column the dashboard selects. Column order is irrelevant to the readers — both
-- sources name the columns they want.
CREATE OR REPLACE VIEW v_current_watchlist AS
SELECT
    w.id                                        AS watchlist_id,
    w.trading_session_id,
    s.trading_date,
    w.instrument_id,
    i.symbol,
    i.trading_symbol,
    i.company_name,
    i.exchange,
    w.current_state,
    w.initial_entry_at,
    w.latest_state_change_at,
    w.initial_entry_price,
    w.latest_price,
    w.latest_score,
    w.latest_rank,
    w.removal_reason,
    w.consecutive_weak_candles,
    w.episode_count,
    w.is_active,
    w.is_simulated,
    snap.vwap,
    snap.ema9,
    snap.ema20,
    snap.relative_volume,
    snap.intraday_change_pct,
    snap.distance_from_vwap_pct,
    snap.ema_spread_pct,
    snap.session_high,
    snap.session_low,
    snap.distance_from_high_pct,
    snap.bullish_qualified,
    snap.reasons,
    snap.failed_reasons,
    snap.data_quality_status                                   AS data_quality,
    snap.warming_up,
    snap.candle_start_at                                       AS latest_candle_at,
    snap.id                                                    AS indicator_snapshot_id,
    snap.rsi_5m,
    snap.rsi_state
FROM daily_watchlists w
JOIN trading_sessions s ON s.id = w.trading_session_id
JOIN instruments i      ON i.id = w.instrument_id
LEFT JOIN indicator_snapshots snap ON snap.id = w.last_indicator_snapshot_id;

-- 5 ── the operator's thresholds this file makes possible are NOT written here.
--
-- ops_settings['alert_strategies'] stays as the operator left it: parse_strategies()
-- supplies the shipped 40/60 pair and the rsi strategy's own defaults for any key the
-- stored document omits, so a saved three-strategy document keeps working unchanged
-- and the first save from /alerts writes all four explicitly.

COMMIT;
