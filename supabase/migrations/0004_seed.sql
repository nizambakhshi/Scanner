-- ============================================================================
-- 0004_seed.sql — ship the strategy defaults declared in the spec (§10, §15.5)
--
-- The row is versioned; edits create a NEW version so historical signals keep
-- pointing at the configuration that produced them.
-- ============================================================================

BEGIN;
SET search_path TO public;

INSERT INTO scanner_configs (
    name, universe, fast_ema_period, slow_ema_period, volume_lookback,
    relative_volume_threshold, alert_cooldown_minutes, removal_confirm_candles,
    feed_staleness_threshold_seconds, candle_grace_seconds, initial_scan_candles,
    max_subscribed_instruments, rank_weights, is_enabled, config_version, created_by
)
SELECT
    'default', 'NIFTY500', 9, 20, 20,
    1.5, 15, 3,
    90, 3, 5,
    500,
    '{"price_strength": 0.30, "relative_volume": 0.30, "vwap_strength": 0.20, "ema_spread": 0.20}'::jsonb,
    TRUE, 1, 'migration'
WHERE NOT EXISTS (
    SELECT 1 FROM scanner_configs WHERE name = 'default' AND config_version = 1
);

COMMIT;
