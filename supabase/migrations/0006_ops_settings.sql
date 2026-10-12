-- 0006: ops_settings — the small key-value table the dashboard writes and the
-- worker reads. The first key is 'retention': {"days": N} is the ageing window
-- the operator controls from the dashboard, and the same number drives both the
-- worker's automatic purge and the manual purge button, so they can never mean
-- two different things.
--
-- Why a separate table instead of a scanner_configs column: scanner_configs is
-- versioned scan configuration (its snapshots must stay interpretable for
-- historical signals); retention is an operations knob that changes often and
-- has no analytical meaning.
--
-- The 0002 pattern applies: RLS enabled with no policies and every grant
-- revoked from anon/authenticated — only the owning role (worker + dashboard
-- server) touches this table.

BEGIN;

CREATE TABLE IF NOT EXISTS ops_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER ops_settings_updated_at BEFORE UPDATE ON ops_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
BEGIN
    EXECUTE 'ALTER TABLE ops_settings ENABLE ROW LEVEL SECURITY';
    EXECUTE 'REVOKE ALL ON ops_settings FROM anon, authenticated';
END $$;

COMMIT;
