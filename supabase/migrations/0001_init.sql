-- ============================================================================
-- 0001_init.sql — Angel One intraday bullish scanner: core schema
--
-- Conventions
--   * every timestamp is TIMESTAMPTZ; the application displays Asia/Kolkata
--   * prices NUMERIC(16,4), volumes BIGINT (share count)
--   * enum-like columns are TEXT + CHECK so a new state never needs a migration
--   * duplicate prevention is enforced by UNIQUE constraints, not application
--     memory (spec §12.3: the database is the final idempotency mechanism)
-- ============================================================================

BEGIN;

SET search_path TO public;

-- ── migrations registry ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── shared updated_at trigger ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── 13.1 instruments ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS instruments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    trading_symbol      TEXT NOT NULL,
    company_name        TEXT,
    instrument_token    TEXT NOT NULL,
    instrument_type     TEXT NOT NULL DEFAULT 'EQ',
    is_nifty500         BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    mapping_status      TEXT NOT NULL DEFAULT 'MAPPED'
                        CHECK (mapping_status IN ('MAPPED','UNMAPPED','AMBIGUOUS',
                                                  'INVALID','STALE_MASTER')),
    last_verified_at    TIMESTAMPTZ,
    previous_close      NUMERIC(16,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT instruments_exchange_token_key UNIQUE (exchange, instrument_token)
);
CREATE UNIQUE INDEX IF NOT EXISTS instruments_equity_symbol_key
    ON instruments (exchange, upper(symbol))
    WHERE instrument_type = 'EQ';
CREATE INDEX IF NOT EXISTS instruments_universe_idx
    ON instruments (is_nifty500, is_active, mapping_status);

CREATE TRIGGER instruments_updated_at BEFORE UPDATE ON instruments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.2 trading_sessions ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading_sessions (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trading_date                DATE NOT NULL,
    exchange                    TEXT NOT NULL DEFAULT 'NSE',
    session_status              TEXT NOT NULL DEFAULT 'PENDING'
                                CHECK (session_status IN ('PENDING','INITIALIZED',
                                                          'IN_PROGRESS','FINALIZED','ABORTED')),
    session_open_at             TIMESTAMPTZ NOT NULL,
    session_close_at            TIMESTAMPTZ NOT NULL,
    initial_scan_completed_at   TIMESTAMPTZ,
    finalized_at                TIMESTAMPTZ,
    stale_data_seconds          INTEGER NOT NULL DEFAULT 0,
    warmup_source               TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trading_sessions_date_key UNIQUE (trading_date, exchange),
    CONSTRAINT trading_session_window_check CHECK (session_close_at > session_open_at)
);

CREATE TRIGGER trading_sessions_updated_at BEFORE UPDATE ON trading_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.3 scanner_configs ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scanner_configs (
    id                                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                                TEXT NOT NULL,
    universe                            TEXT NOT NULL DEFAULT 'NIFTY500',
    fast_ema_period                     INTEGER NOT NULL CHECK (fast_ema_period >= 2),
    slow_ema_period                     INTEGER NOT NULL CHECK (slow_ema_period > 2),
    volume_lookback                     INTEGER NOT NULL CHECK (volume_lookback >= 2),
    relative_volume_threshold           NUMERIC(8,4) NOT NULL CHECK (relative_volume_threshold > 0),
    alert_cooldown_minutes              INTEGER NOT NULL CHECK (alert_cooldown_minutes >= 0),
    removal_confirm_candles             INTEGER NOT NULL CHECK (removal_confirm_candles >= 1),
    feed_staleness_threshold_seconds    INTEGER NOT NULL CHECK (feed_staleness_threshold_seconds >= 5),
    candle_grace_seconds                INTEGER NOT NULL CHECK (candle_grace_seconds BETWEEN 0 AND 30),
    initial_scan_candles                INTEGER NOT NULL CHECK (initial_scan_candles >= 1),
    max_subscribed_instruments          INTEGER NOT NULL CHECK (max_subscribed_instruments BETWEEN 1 AND 1000),
    rank_weights                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_enabled                          BOOLEAN NOT NULL DEFAULT TRUE,
    config_version                      INTEGER NOT NULL DEFAULT 1,
    created_by                          TEXT NOT NULL DEFAULT 'system',
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scanner_config_periods_check CHECK (slow_ema_period > fast_ema_period)
);
CREATE UNIQUE INDEX IF NOT EXISTS scanner_configs_name_version_key
    ON scanner_configs (name, config_version);

CREATE TRIGGER scanner_configs_updated_at BEFORE UPDATE ON scanner_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.4 candles_1m ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS candles_1m (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id           BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    trading_date            DATE NOT NULL,
    candle_start_at         TIMESTAMPTZ NOT NULL,
    open                    NUMERIC(16,4) NOT NULL,
    high                    NUMERIC(16,4) NOT NULL,
    low                     NUMERIC(16,4) NOT NULL,
    close                   NUMERIC(16,4) NOT NULL,
    volume                  BIGINT NOT NULL CHECK (volume >= 0),
    is_complete             BOOLEAN NOT NULL DEFAULT FALSE,
    data_quality_status     TEXT NOT NULL DEFAULT 'INCOMPLETE'
                            CHECK (data_quality_status IN ('OK','PARTIAL','WARMING_UP',
                                                           'STALE','INCOMPLETE','NO_TRADE','SUSPECT')),
    source_timestamp        TIMESTAMPTZ,
    tick_count              INTEGER NOT NULL DEFAULT 0,
    is_simulated            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one row per instrument per minute: the candle bucket is the identity
    CONSTRAINT candles_1m_instrument_minute_key UNIQUE (instrument_id, candle_start_at),
    CONSTRAINT candles_1m_ohlc_check CHECK (high >= low AND high >= open AND high >= close
                                            AND low <= open AND low <= close)
);
CREATE INDEX IF NOT EXISTS candles_1m_date_instrument_idx
    ON candles_1m (trading_date, instrument_id, candle_start_at);
CREATE INDEX IF NOT EXISTS candles_1m_complete_idx
    ON candles_1m (candle_start_at DESC) WHERE is_complete;

CREATE TRIGGER candles_1m_updated_at BEFORE UPDATE ON candles_1m
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.5 indicator_snapshots ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id           BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    trading_date            DATE NOT NULL,
    candle_start_at         TIMESTAMPTZ NOT NULL,
    close                   NUMERIC(16,4) NOT NULL,
    previous_close          NUMERIC(16,4),
    vwap                    NUMERIC(16,4),
    ema9                    NUMERIC(16,4),
    ema20                   NUMERIC(16,4),
    average_volume20        NUMERIC(16,4),
    relative_volume         NUMERIC(8,4),
    intraday_change_pct     NUMERIC(8,4),
    distance_from_vwap_pct  NUMERIC(8,4),
    ema_spread_pct          NUMERIC(8,4),
    session_high            NUMERIC(16,4),
    session_low             NUMERIC(16,4),
    distance_from_high_pct  NUMERIC(8,4),
    bullish_qualified       BOOLEAN NOT NULL DEFAULT FALSE,
    reasons                 TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    failed_reasons          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    data_quality_status     TEXT NOT NULL DEFAULT 'OK',
    warming_up              BOOLEAN NOT NULL DEFAULT TRUE,
    score                   NUMERIC(6,2),
    rank                    INTEGER,
    config_version          INTEGER NOT NULL DEFAULT 1,
    is_simulated            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- reprocessing a candle must UPDATE this row, never add a second one
    CONSTRAINT indicator_snapshots_instrument_minute_key
        UNIQUE (instrument_id, candle_start_at)
);
CREATE INDEX IF NOT EXISTS indicator_snapshots_qualifying_idx
    ON indicator_snapshots (trading_date, bullish_qualified, candle_start_at DESC);

-- ── 13.6 daily_watchlists ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_watchlists (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trading_session_id          BIGINT NOT NULL REFERENCES trading_sessions(id) ON DELETE CASCADE,
    instrument_id               BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    current_state               TEXT NOT NULL CHECK (current_state IN ('WARMING_UP','ACTIVE',
                                                                       'WEAKENING','REMOVED',
                                                                       'REQUALIFIED','DATA_STALE')),
    initial_entry_at            TIMESTAMPTZ,
    latest_state_change_at      TIMESTAMPTZ,
    initial_entry_price         NUMERIC(16,4),
    latest_price                NUMERIC(16,4),
    latest_score                NUMERIC(6,2),
    latest_rank                 INTEGER,
    removal_reason              TEXT,
    consecutive_weak_candles    INTEGER NOT NULL DEFAULT 0,
    episode_count               INTEGER NOT NULL DEFAULT 1,
    last_indicator_snapshot_id  BIGINT REFERENCES indicator_snapshots(id) ON DELETE SET NULL,
    is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
    is_simulated                BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT daily_watchlists_session_instrument_key
        UNIQUE (trading_session_id, instrument_id)
);
CREATE INDEX IF NOT EXISTS daily_watchlists_active_idx
    ON daily_watchlists (trading_session_id, current_state, latest_score DESC NULLS LAST);

CREATE TRIGGER daily_watchlists_updated_at BEFORE UPDATE ON daily_watchlists
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.7 watchlist_events (append-only history) ────────────────────────────
CREATE TABLE IF NOT EXISTS watchlist_events (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    watchlist_id            BIGINT NOT NULL REFERENCES daily_watchlists(id) ON DELETE CASCADE,
    event_type              TEXT NOT NULL,
    previous_state          TEXT,
    new_state               TEXT NOT NULL,
    event_timestamp         TIMESTAMPTZ NOT NULL,
    reason                  TEXT NOT NULL,
    price                   NUMERIC(16,4),
    indicator_snapshot_id   BIGINT REFERENCES indicator_snapshots(id) ON DELETE SET NULL,
    detail                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS watchlist_events_timeline_idx
    ON watchlist_events (watchlist_id, event_timestamp);

-- ── 13.8 signals ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id           BIGINT REFERENCES instruments(id) ON DELETE SET NULL,
    trading_session_id      BIGINT REFERENCES trading_sessions(id) ON DELETE SET NULL,
    signal_type             TEXT NOT NULL CHECK (signal_type IN ('BULLISH_ENTRY',
                                                                'WATCHLIST_ADDED',
                                                                'WATCHLIST_WEAKENING',
                                                                'WATCHLIST_REMOVED',
                                                                'WATCHLIST_REQUALIFIED',
                                                                'DATA_FEED_INTERRUPTED',
                                                                'DATA_FEED_RECOVERED',
                                                                'DAILY_SCAN_COMPLETE')),
    qualifying_episode_id   TEXT NOT NULL,
    candle_start_at         TIMESTAMPTZ,
    event_timestamp         TIMESTAMPTZ NOT NULL,
    price                   NUMERIC(16,4),
    previous_close          NUMERIC(16,4),
    intraday_change_pct     NUMERIC(8,4),
    vwap                    NUMERIC(16,4),
    ema9                    NUMERIC(16,4),
    ema20                   NUMERIC(16,4),
    relative_volume         NUMERIC(8,4),
    score                   NUMERIC(6,2),
    rank                    INTEGER,
    reasons                 TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    message                 TEXT,
    idempotency_key         TEXT NOT NULL,
    config_version          INTEGER NOT NULL DEFAULT 1,
    is_simulated            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FINAL duplicate prevention: retry/restart cannot create a second row
    CONSTRAINT signals_idempotency_key_key UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS signals_session_time_idx
    ON signals (trading_session_id, event_timestamp DESC);
CREATE INDEX IF NOT EXISTS signals_instrument_time_idx
    ON signals (instrument_id, event_timestamp DESC);
CREATE INDEX IF NOT EXISTS signals_type_time_idx
    ON signals (signal_type, event_timestamp DESC);

-- ── 13.9 notification_deliveries ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    signal_id               BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    channel                 TEXT NOT NULL CHECK (channel IN ('TELEGRAM','DASHBOARD')),
    destination_reference   TEXT,
    delivery_status         TEXT NOT NULL CHECK (delivery_status IN ('QUEUED','IN_FLIGHT',
                                                                     'DELIVERED','FAILED',
                                                                     'PERMANENTLY_FAILED',
                                                                     'SUPPRESSED','SIMULATED')),
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    next_retry_at           TIMESTAMPTZ,
    last_attempt_at         TIMESTAMPTZ,
    delivered_at            TIMESTAMPTZ,
    provider_message_id     TEXT,
    last_error              TEXT,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one queue row per signal per channel; retries update it in place
    CONSTRAINT notification_channel_once_key UNIQUE (signal_id, channel)
);
CREATE INDEX IF NOT EXISTS notification_pending_idx
    ON notification_deliveries (delivery_status, next_retry_at NULLS FIRST);

CREATE TRIGGER notification_deliveries_updated_at BEFORE UPDATE ON notification_deliveries
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.10 worker_heartbeats ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    id                              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    worker_id                       TEXT NOT NULL,
    status                          TEXT NOT NULL,
    feed_status                     TEXT NOT NULL DEFAULT 'DISCONNECTED',
    last_market_data_at             TIMESTAMPTZ,
    last_completed_candle_at        TIMESTAMPTZ,
    subscribed_instrument_count     INTEGER NOT NULL DEFAULT 0,
    monitored_instrument_count      INTEGER NOT NULL DEFAULT 0,
    universe_instrument_count       INTEGER NOT NULL DEFAULT 0,
    mapped_instrument_count         INTEGER NOT NULL DEFAULT 0,
    active_watchlist_count          INTEGER NOT NULL DEFAULT 0,
    pending_notification_count      INTEGER NOT NULL DEFAULT 0,
    queue_depth                     INTEGER NOT NULL DEFAULT 0,
    processing_latency_ms           NUMERIC(10,2),
    last_error                      TEXT,
    scan_mode                       TEXT NOT NULL DEFAULT 'live',
    simulation_label                BOOLEAN NOT NULL DEFAULT FALSE,
    started_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT worker_heartbeats_worker_key UNIQUE (worker_id)
);

CREATE TRIGGER worker_heartbeats_updated_at BEFORE UPDATE ON worker_heartbeats
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── 13.11 audit_logs ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor           TEXT NOT NULL,
    actor_role      TEXT,
    action          TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       TEXT,
    previous_value  JSONB,
    new_value       JSONB,
    request_ip      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_logs_time_idx ON audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_entity_idx ON audit_logs (entity_type, entity_id);

-- ── worker coordination lease (multi-replica safety, §17 concurrency) ──────
CREATE TABLE IF NOT EXISTS worker_leases (
    lease_name  TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- ── §16 authentication: single-user now, role-ready later ──────────────────
CREATE TABLE IF NOT EXISTS app_users (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'SUPER_ADMIN'
                    CHECK (role IN ('SUPER_ADMIN','ADMIN','USER')),
    display_name    TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT app_users_email_key UNIQUE (email)
);

CREATE TRIGGER app_users_updated_at BEFORE UPDATE ON app_users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS app_sessions (
    id              TEXT PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_ip      TEXT,
    user_agent      TEXT
);
CREATE INDEX IF NOT EXISTS app_sessions_expiry_idx ON app_sessions (expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           TEXT NOT NULL,
    request_ip      TEXT,
    succeeded       BOOLEAN NOT NULL DEFAULT FALSE,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_lookup_idx
    ON login_attempts (email, attempted_at DESC);

-- ── daily summaries (§13 persistence list) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_summaries (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trading_session_id          BIGINT NOT NULL REFERENCES trading_sessions(id) ON DELETE CASCADE,
    trading_date                DATE NOT NULL,
    universe_count              INTEGER NOT NULL DEFAULT 0,
    mapped_count                INTEGER NOT NULL DEFAULT 0,
    subscribed_count            INTEGER NOT NULL DEFAULT 0,
    monitored_count             INTEGER NOT NULL DEFAULT 0,
    candles_persisted           INTEGER NOT NULL DEFAULT 0,
    qualified_count             INTEGER NOT NULL DEFAULT 0,
    warming_up_count            INTEGER NOT NULL DEFAULT 0,
    excluded_count              INTEGER NOT NULL DEFAULT 0,
    signals_generated           INTEGER NOT NULL DEFAULT 0,
    telegram_delivered          INTEGER NOT NULL DEFAULT 0,
    telegram_failed             INTEGER NOT NULL DEFAULT 0,
    stale_data_seconds          INTEGER NOT NULL DEFAULT 0,
    final_watchlist             JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes                       TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT daily_summaries_date_key UNIQUE (trading_date)
);

CREATE TRIGGER daily_summaries_updated_at BEFORE UPDATE ON daily_summaries
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
