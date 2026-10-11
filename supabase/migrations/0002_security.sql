-- ============================================================================
-- 0002_security.sql — lock the tables down to server-side roles only
--
-- Supabase grants anonymous/authenticated PostgREST access to `public` tables
-- by default. Nothing in this product should ever read market data or user
-- rows through an anon key: the dashboard talks to PostgreSQL from Next.js
-- server code with a direct role, and the worker uses its own connection.
--
-- Therefore: enable RLS with NO policies (deny-by-default for anon /
-- authenticated) and revoke table privileges from those roles. The owning
-- role (postgres / service_role) is unaffected.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'instruments', 'trading_sessions', 'scanner_configs', 'candles_1m',
        'indicator_snapshots', 'daily_watchlists', 'watchlist_events', 'signals',
        'notification_deliveries', 'worker_heartbeats', 'audit_logs', 'worker_leases',
        'app_users', 'app_sessions', 'login_attempts', 'daily_summaries', 'schema_migrations'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
    END LOOP;
END
$$;

-- PostgREST should not surface this schema at all even to an authenticated key.
REVOKE USAGE ON SCHEMA public FROM anon;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;

COMMIT;
