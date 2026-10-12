-- 0008_daily_rsi.sql — daily/weekly candles, per-stock detail sheet, RSI signal types
--
-- Three additive changes; no existing market data is touched.
--
-- 1. candles_1d — broker daily bars (ONE_DAY), corporate-action adjusted, with
--    a Wilder RSI-14 column computed by scanner-sync-daily over the full
--    series. This is the single source for: daily RSI, weekly RSI (resampled
--    in code), 52-week high/low and the all-time high/low shown on /stocks.
--    Retention deliberately does NOT purge these: they are ~250 rows per
--    instrument per year and are the historical memory the intraday tables
--    are not allowed to keep.
--
-- 2. candles_1w — the same bars resampled to ISO weeks (Monday start), also
--    with rsi14, so the dashboard never has to resample in SQL.
--
-- 3. stock_details — one row per instrument: fundamentals scraped by
--    scanner-sync-profiles (P/E, market cap, listing date) plus the RSI
--    snapshot and 52W/ATH/ATL computed by scanner-sync-daily. rsi_bias is the
--    classification (RSI_BULLISH / RSI_BEARISH / NEUTRAL, thresholds 60/40 per
--    the operator's spec); last_alert_bias remembers what the worker last
--    alerted so a signal fires only when the classification *changes*.
--
-- 4. signals.signal_type gains the two RSI classification events. The CHECK
--    from 0001 is dropped and re-created with the full list — a row written
--    under the old list still validates under the new one.
--
-- Rollout notes:
-- * all three tables follow 0002_security.sql / 0005: RLS on, no policies,
--   privileges revoked from anon/authenticated — only the owning server roles;
-- * scanner-migrate records the version row itself, so this file must not.

BEGIN;

CREATE TABLE IF NOT EXISTS candles_1d (
    instrument_id  BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    trading_date   DATE NOT NULL,
    open           NUMERIC(16,4) NOT NULL,
    high           NUMERIC(16,4) NOT NULL,
    low            NUMERIC(16,4) NOT NULL,
    close          NUMERIC(16,4) NOT NULL,
    volume         BIGINT NOT NULL DEFAULT 0,
    -- Wilder RSI-14 over the instrument's adjusted close series; NULL for the
    -- first 14 bars and for bars whose instrument has too little history.
    rsi14          NUMERIC(8,4),
    source         TEXT NOT NULL DEFAULT 'angel-one-day',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, trading_date)
);
CREATE INDEX IF NOT EXISTS candles_1d_date_idx ON candles_1d (trading_date);

CREATE TABLE IF NOT EXISTS candles_1w (
    instrument_id  BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    -- Monday of the ISO week the candle belongs to; the newest row may be a
    -- partial week (its close is the latest daily close), which is what a
    -- weekly RSI is normally computed on mid-week.
    week_start     DATE NOT NULL,
    open           NUMERIC(16,4) NOT NULL,
    high           NUMERIC(16,4) NOT NULL,
    low            NUMERIC(16,4) NOT NULL,
    close          NUMERIC(16,4) NOT NULL,
    volume         BIGINT NOT NULL DEFAULT 0,
    rsi14          NUMERIC(8,4),
    source         TEXT NOT NULL DEFAULT 'resampled-from-candles-1d',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, week_start)
);

CREATE TABLE IF NOT EXISTS stock_details (
    instrument_id      BIGINT PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
    -- Fundamentals (scanner-sync-profiles). NULL means "not fetched", never 0.
    pe_ratio           NUMERIC(12,4),
    market_cap_cr      NUMERIC(18,4),
    listing_date       DATE,
    profile_source     TEXT,
    profile_as_of      TIMESTAMPTZ,
    -- Computed from candles_1d (scanner-sync-daily).
    high_52w           NUMERIC(16,4),
    low_52w            NUMERIC(16,4),
    ath                NUMERIC(16,4),
    atl                NUMERIC(16,4),
    data_start         DATE,
    daily_rsi          NUMERIC(8,4),
    weekly_rsi         NUMERIC(8,4),
    rsi_bias           TEXT CHECK (rsi_bias IN ('BULLISH','BEARISH','NEUTRAL')),
    rsi_as_of          DATE,
    -- What the worker has already alerted; NULL = never classified yet, so
    -- the first observed classification is a silent baseline, not an alert.
    last_alert_bias    TEXT CHECK (last_alert_bias IN ('BULLISH','BEARISH','NEUTRAL')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER candles_1d_updated_at BEFORE UPDATE ON candles_1d
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER candles_1w_updated_at BEFORE UPDATE ON candles_1w
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER stock_details_updated_at BEFORE UPDATE ON stock_details
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

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
                    'RSI_BEARISH')
);

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['candles_1d', 'candles_1w', 'stock_details']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
    END LOOP;
END
$$;

COMMIT;
