-- 0007_fno_coverage.sql — surface the F&O scan subset on the dashboard
--
-- Two truths the board could not express after the F&O cutover:
--
-- * The coverage view counted only NIFTY500 mapping. With the worker scanning
--   the F&O subset, the board showed "Universe 494" with nothing telling the
--   operator that only 212 underlyings are actually scanned. The extra column
--   is appended (not inserted) so CREATE OR REPLACE VIEW stays legal.
-- * scanner_configs.universe still said NIFTY500 while the worker runs
--   UNIVERSE=FNO from its environment; the config page displays this row, so
--   it must agree with what actually runs.

BEGIN;
SET search_path TO public;

CREATE OR REPLACE VIEW v_instrument_coverage AS
SELECT
    COUNT(*) FILTER (WHERE is_nifty500)                                    AS nifty500_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'MAPPED')       AS mapped_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'UNMAPPED')     AS unmapped_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'AMBIGUOUS')    AS ambiguous_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND mapping_status = 'INVALID')      AS invalid_count,
    COUNT(*) FILTER (WHERE is_nifty500 AND is_active)                       AS active_universe_count,
    MAX(last_verified_at)                                                   AS last_verified_at,
    COUNT(*) FILTER (WHERE is_fno)                                          AS fno_count
FROM instruments;

UPDATE scanner_configs SET universe = 'FNO' WHERE is_enabled AND universe <> 'FNO';

COMMIT;
