-- 0005_fno_and_sector_indices.sql — F&O universe flag + Nifty sector tile tables
--
-- Two additive changes; no market data is touched or backfilled.
--
-- 1. instruments.is_fno — the worker's API budget is spent on the F&O set
--    (212 NSE equities in the 2026-10-12 scrip master) instead of the full
--    NIFTY500: UNIVERSE=FNO filters on this flag, and sync_instruments
--    recomputes it from the NFO FUTSTK/OPTSTK underlyings on every run.
--
-- 2. sector_indices + sector_quotes — the dashboard's sector tile shows Nifty
--    sector indices streaming off the same smart-stream socket the stocks use.
--    sector_quotes is a live scratchpad (one upserted row per index, no
--    history): a tile that wants history can read candles, not this table.
--
-- Rollout notes:
-- * both new tables follow 0002_security.sql: RLS on, no policies, privileges
--   revoked from anon/authenticated — only the owning server roles read them;
-- * scanner-migrate records the version row itself, so this file must not.

BEGIN;

ALTER TABLE instruments
    ADD COLUMN IF NOT EXISTS is_fno BOOLEAN NOT NULL DEFAULT FALSE;

-- The one filter the worker's hot path runs at every session start.
CREATE INDEX IF NOT EXISTS instruments_fno_universe_idx
    ON instruments (symbol)
    WHERE is_fno AND is_active AND mapping_status = 'MAPPED';

CREATE TABLE IF NOT EXISTS sector_indices (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_symbol     TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    instrument_token TEXT NOT NULL UNIQUE,
    display_order    INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sector_quotes (
    index_symbol      TEXT PRIMARY KEY,
    last_price        NUMERIC(16,4) NOT NULL,
    previous_close    NUMERIC(16,4),
    change_pct        NUMERIC(8,4),
    source_updated_at TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER sector_indices_updated_at BEFORE UPDATE ON sector_indices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER sector_quotes_updated_at BEFORE UPDATE ON sector_quotes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sector_indices', 'sector_quotes']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
    END LOOP;
END
$$;

COMMIT;
