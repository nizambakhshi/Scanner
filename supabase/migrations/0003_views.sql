-- ============================================================================
-- 0003_views.sql — read models used by the dashboard API
--
-- Keeping the joins here means the Next.js route handlers issue one simple
-- SELECT per panel, and coverage numbers cannot disagree between pages.
-- ============================================================================

BEGIN;
SET search_path TO public;

-- Universe / subscription coverage (spec §5.2: never imply full coverage
-- when only a subset is monitored).
CREATE OR REPLACE VIEW v_instrument_coverage AS
SELECT
    COUNT(*) FILTER (WHERE is_nifty500)                                    AS nifty500_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'MAPPED')       AS mapped_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'UNMAPPED')     AS unmapped_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'AMBIGUOUS')    AS ambiguous_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'INVALID')      AS invalid_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND is_active)                       AS active_universe_count,
    MAX(last_verified_at)                                                   AS last_verified_at
FROM instruments;

-- The current daily watchlist with the most recent indicator row attached.
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
    snap.id                                                    AS indicator_snapshot_id
FROM daily_watchlists w
JOIN trading_sessions s ON s.id = w.trading_session_id
JOIN instruments i      ON i.id = w.instrument_id
LEFT JOIN indicator_snapshots snap ON snap.id = w.last_indicator_snapshot_id;

-- Signals with delivery state rolled up per channel.
CREATE OR REPLACE VIEW v_signal_feed AS
SELECT
    sig.id,
    sig.signal_type,
    sig.event_timestamp,
    sig.candle_start_at,
    sig.price,
    sig.previous_close,
    sig.intraday_change_pct,
    sig.vwap,
    sig.ema9,
    sig.ema20,
    sig.relative_volume,
    sig.score,
    sig.rank,
    sig.reasons,
    sig.message,
    sig.config_version,
    sig.is_simulated,
    sig.trading_session_id,
    s.trading_date,
    sig.instrument_id,
    i.symbol,
    i.trading_symbol,
    i.company_name,
    i.exchange,
    bool_or(nd.channel = 'TELEGRAM' AND nd.delivery_status = 'DELIVERED')   AS telegram_delivered,
    bool_and(nd.channel = 'TELEGRAM' AND nd.delivery_status IN
             ('FAILED','PERMANENTLY_FAILED'))                               AS telegram_failed,
    max(CASE WHEN nd.channel = 'TELEGRAM' THEN nd.delivery_status END)      AS telegram_status,
    max(CASE WHEN nd.channel = 'TELEGRAM' THEN nd.attempt_count END)        AS telegram_attempts,
    max(CASE WHEN nd.channel = 'TELEGRAM' THEN nd.last_error END)           AS telegram_error
FROM signals sig
LEFT JOIN trading_sessions s          ON s.id = sig.trading_session_id
LEFT JOIN instruments i               ON i.id = sig.instrument_id
LEFT JOIN notification_deliveries nd  ON nd.signal_id = sig.id
GROUP BY sig.id, s.trading_date, i.symbol, i.trading_symbol, i.company_name, i.exchange;

-- Newest worker status per host, for the health panel.
CREATE OR REPLACE VIEW v_worker_status AS
SELECT DISTINCT ON (worker_id)
    worker_id, status, feed_status, last_market_data_at, last_completed_candle_at,
    subscribed_instrument_count, monitored_instrument_count, universe_instrument_count,
    mapped_instrument_count, active_watchlist_count, pending_notification_count,
    queue_depth, processing_latency_ms, last_error, scan_mode, simulation_label,
    started_at, updated_at
FROM worker_heartbeats
ORDER BY worker_id, updated_at DESC;

-- One row per trading day for the history page.
CREATE OR REPLACE VIEW v_session_history AS
SELECT
    s.id AS trading_session_id,
    s.trading_date,
    s.session_status,
    s.session_open_at,
    s.session_close_at,
    s.initial_scan_completed_at,
    s.finalized_at,
    s.stale_data_seconds,
    COALESCE(d.universe_count, 0)   AS universe_count,
    COALESCE(d.mapped_count, 0)     AS mapped_count,
    COALESCE(d.subscribed_count, 0) AS subscribed_count,
    COALESCE(d.monitored_count, 0)  AS monitored_count,
    COALESCE(d.qualified_count, 0)  AS qualified_count,
    COALESCE(d.signals_generated,0) AS signals_generated,
    COALESCE(d.telegram_delivered,0) AS telegram_delivered,
    COALESCE(d.telegram_failed,0)   AS telegram_failed,
    d.notes
FROM trading_sessions s
LEFT JOIN daily_summaries d ON d.trading_session_id = s.id;

COMMIT;
