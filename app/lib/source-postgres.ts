/**
 * The PostgreSQL source: one SELECT per panel, reading the read-model views from
 * `0003_views.sql`.
 *
 * The joins live in the database on purpose — coverage numbers must not be able to
 * disagree between two pages because two files built them with two slightly
 * different `WHERE` clauses. Every numeric column is cast in SQL, because
 * `NUMERIC` and `COUNT(*)` arrive over the wire as strings, and a page that prints
 * `"1.5000"` as a price is a bug nobody sees until the numbers are wrong.
 *
 * No query here can return `instrument_token`, a `destination_reference`, a
 * password hash, or a session token: the column lists are explicit, so a future
 * column added to a table cannot leak into a response by accident (§19).
 */

import { query } from "./db";
import {
  dateParam,
  mapCandle,
  mapConfig,
  mapCoverage,
  mapDelivery,
  mapEvent,
  mapHistory,
  mapInstrument,
  mapSectorQuote,
  mapSignal,
  mapSnapshot,
  mapWatchlist,
  mapWorker,
  type Raw,
} from "./rows";
import type {
  InitialScanSummary,
  InstrumentDetail,
  ScannerSource,
  SignalFilter,
  SourceMeta,
} from "./types";
import { jsonRecord, toIst } from "./format";

const COVERAGE_SQL = `
  SELECT nifty500_count::int AS nifty500_count,
         mapped_count::int    AS mapped_count,
         unmapped_count::int  AS unmapped_count,
         ambiguous_count::int AS ambiguous_count,
         invalid_count::int   AS invalid_count,
         active_universe_count::int AS active_universe_count,
         fno_count::int       AS fno_count,
         last_verified_at     AS last_verified_at
  FROM v_instrument_coverage`;

const WORKER_SQL = `
  SELECT worker_id, status, feed_status,
         last_market_data_at, last_completed_candle_at,
         subscribed_instrument_count::int AS subscribed_instrument_count,
         monitored_instrument_count::int  AS monitored_instrument_count,
         universe_instrument_count::int   AS universe_instrument_count,
         mapped_instrument_count::int     AS mapped_instrument_count,
         active_watchlist_count::int      AS active_watchlist_count,
         pending_notification_count::int  AS pending_notification_count,
         queue_depth::int                 AS queue_depth,
         processing_latency_ms::float8    AS processing_latency_ms,
         last_error, scan_mode, simulation_label, started_at, updated_at
  FROM v_worker_status
  ORDER BY updated_at DESC NULLS LAST`;

/** Qualifiers first, then by score — the same order the watchlist ranks in (§11.1). */
const WATCHLIST_SQL = `
  SELECT watchlist_id::int AS watchlist_id, trading_session_id::int AS trading_session_id,
         trading_date::text AS trading_date, instrument_id::int AS instrument_id,
         symbol, trading_symbol, company_name, exchange, current_state,
         initial_entry_at, latest_state_change_at,
         vwap::float8 AS vwap, ema9::float8 AS ema9, ema20::float8 AS ema20,
         relative_volume::float8 AS relative_volume,
         intraday_change_pct::float8 AS intraday_change_pct,
         distance_from_vwap_pct::float8 AS distance_from_vwap_pct,
         ema_spread_pct::float8 AS ema_spread_pct,
         session_high::float8 AS session_high, session_low::float8 AS session_low,
         distance_from_high_pct::float8 AS distance_from_high_pct,
         initial_entry_price::float8 AS initial_entry_price,
         latest_price::float8 AS latest_price, latest_score::float8 AS latest_score,
         latest_rank::int AS latest_rank, removal_reason,
         consecutive_weak_candles::int AS consecutive_weak_candles,
         episode_count::int AS episode_count,
         is_active, is_simulated, bullish_qualified, reasons, failed_reasons,
         data_quality, warming_up, latest_candle_at,
         indicator_snapshot_id::int AS indicator_snapshot_id
  FROM v_current_watchlist
  WHERE ($1::date IS NULL OR trading_date = $1::date)
    AND ($2::text IS NULL OR upper(symbol) = upper($2::text))
  ORDER BY (current_state IN ('ACTIVE','REQUALIFIED')) DESC,
           latest_score DESC NULLS LAST,
           symbol`;

const SIGNALS_SQL = `
  SELECT id::int AS id, signal_type, event_timestamp, candle_start_at,
         price::float8 AS price, previous_close::float8 AS previous_close,
         intraday_change_pct::float8 AS intraday_change_pct, vwap::float8 AS vwap,
         ema9::float8 AS ema9, ema20::float8 AS ema20,
         relative_volume::float8 AS relative_volume, score::float8 AS score,
         rank::int AS rank, reasons, message, config_version::int AS config_version,
         is_simulated, trading_session_id::int AS trading_session_id,
         trading_date::text AS trading_date, instrument_id::int AS instrument_id,
         symbol, trading_symbol, company_name, exchange,
         COALESCE(telegram_delivered, false) AS telegram_delivered,
         COALESCE(telegram_failed, false)    AS telegram_failed,
         telegram_status, COALESCE(telegram_attempts, 0)::int AS telegram_attempts,
         telegram_error
  FROM v_signal_feed
  WHERE ($1::date IS NULL OR trading_date = $1::date)
    AND ($2::text IS NULL OR signal_type = $2::text)
    AND ($3::text IS NULL OR upper(symbol) = upper($3::text))
  ORDER BY event_timestamp DESC
  LIMIT $4::int`;

const HISTORY_SQL = `
  SELECT trading_session_id::int AS trading_session_id, trading_date::text AS trading_date,
         session_status, session_open_at, session_close_at, initial_scan_completed_at, finalized_at,
         stale_data_seconds::int AS stale_data_seconds,
         universe_count::int AS universe_count, mapped_count::int AS mapped_count,
         subscribed_count::int AS subscribed_count, monitored_count::int AS monitored_count,
         qualified_count::int AS qualified_count, signals_generated::int AS signals_generated,
         telegram_delivered::int AS telegram_delivered, telegram_failed::int AS telegram_failed,
         notes
  FROM v_session_history
  ORDER BY trading_date DESC
  LIMIT $1::int`;

const CONFIG_SQL = `
  SELECT id::int AS id, name, is_enabled, config_version::int AS config_version, universe,
         fast_ema_period::int AS fast_ema_period, slow_ema_period::int AS slow_ema_period,
         volume_lookback::int AS volume_lookback,
         relative_volume_threshold::float8 AS relative_volume_threshold,
         alert_cooldown_minutes::int AS alert_cooldown_minutes,
         removal_confirm_candles::int AS removal_confirm_candles,
         feed_staleness_threshold_seconds::int AS feed_staleness_threshold_seconds,
         candle_grace_seconds::int AS candle_grace_seconds,
         initial_scan_candles::int AS initial_scan_candles,
         max_subscribed_instruments::int AS max_subscribed_instruments,
         rank_weights
  FROM scanner_configs
  ORDER BY is_enabled DESC, config_version DESC
  LIMIT 1`;

/** The initial-scan counts are journaled by the worker as an audit event; there is
 *  no fixed-column table that can hold them (§13.12). */
const INITIAL_SCAN_SQL = `
  SELECT new_value, created_at
  FROM audit_logs
  WHERE action = 'initial_scan'
  ORDER BY created_at DESC
  LIMIT 1`;

const INSTRUMENT_SQL = `
  SELECT id::int AS id, exchange, symbol, trading_symbol, company_name, instrument_type,
         is_nifty500, is_active, mapping_status,
         previous_close::float8 AS previous_close, last_verified_at
  FROM instruments
  WHERE upper(symbol) = upper($1::text) OR upper(trading_symbol) = upper($1::text)
  ORDER BY last_verified_at DESC NULLS LAST
  LIMIT 1`;

const CANDLES_SQL = `
  SELECT instrument_id::int AS instrument_id, trading_date::text AS trading_date,
         candle_start_at, open::float8 AS open, high::float8 AS high, low::float8 AS low,
         close::float8 AS close, volume::int8 AS volume, is_complete, data_quality_status,
         source_timestamp, tick_count::int AS tick_count, is_simulated
  FROM candles_1m
  WHERE instrument_id = $1::int
  ORDER BY candle_start_at DESC
  LIMIT $2::int`;

const SNAPSHOTS_SQL = `
  SELECT id::int AS id, instrument_id::int AS instrument_id, trading_date::text AS trading_date,
         candle_start_at, close::float8 AS close, previous_close::float8 AS previous_close,
         vwap::float8 AS vwap, ema9::float8 AS ema9, ema20::float8 AS ema20,
         average_volume20::float8 AS average_volume20,
         relative_volume::float8 AS relative_volume,
         intraday_change_pct::float8 AS intraday_change_pct,
         distance_from_vwap_pct::float8 AS distance_from_vwap_pct,
         ema_spread_pct::float8 AS ema_spread_pct,
         session_high::float8 AS session_high, session_low::float8 AS session_low,
         distance_from_high_pct::float8 AS distance_from_high_pct,
         bullish_qualified, reasons, failed_reasons, data_quality_status, warming_up,
         score::float8 AS score, rank::int AS rank, config_version::int AS config_version,
         is_simulated
  FROM indicator_snapshots
  WHERE instrument_id = $1::int
  ORDER BY candle_start_at DESC
  LIMIT $2::int`;

const EVENTS_SQL = `
  SELECT we.id::int AS id, we.watchlist_id::int AS watchlist_id, i.symbol,
         we.event_type, we.previous_state, we.new_state, we.event_timestamp, we.reason,
         we.price::float8 AS price,
         we.indicator_snapshot_id::int AS indicator_snapshot_id, we.detail
  FROM watchlist_events we
  JOIN daily_watchlists w ON w.id = we.watchlist_id
  JOIN instruments i      ON i.id = w.instrument_id
  WHERE ($1::int IS NULL OR we.watchlist_id = $1::int)
    AND ($2::int IS NULL OR w.instrument_id = $2::int)
  ORDER BY we.event_timestamp DESC
  LIMIT $3::int`;

const DELIVERIES_SQL = `
  SELECT id::int AS id, signal_id::int AS signal_id, channel, delivery_status,
         attempt_count::int AS attempt_count, next_retry_at, last_attempt_at, delivered_at,
         provider_message_id, last_error,
         (channel = 'DASHBOARD') AS is_simulated
  FROM notification_deliveries
  WHERE signal_id = ANY($1::int[])
  ORDER BY id`;

/** Sector tile: the fixed index list LEFT JOINed to its newest quote. The LEFT
 *  JOIN is deliberate — an index the worker has not flushed yet still renders
 *  as "waiting" instead of silently vanishing from the tile. No broker token
 *  leaves the database (§19). */
const SECTOR_SQL = `
  SELECT si.index_symbol, si.display_name, si.display_order::int AS display_order,
         sq.last_price::float8     AS last_price,
         sq.previous_close::float8 AS previous_close,
         sq.change_pct::float8     AS change_pct,
         sq.source_updated_at
  FROM sector_indices si
  LEFT JOIN sector_quotes sq ON sq.index_symbol = si.index_symbol
  ORDER BY si.display_order, si.index_symbol`;

function limit(value: number | undefined, max: number): number {
  return Math.min(Math.max(Math.trunc(value ?? max), 1), max);
}

export const postgresSource: ScannerSource = {
  name: "postgres",

  async meta(): Promise<SourceMeta> {
    return {
      source: "postgres",
      // Live data is not simulated. Individual rows still carry is_simulated, and
      // each panel shows that flag where it matters — a table cannot be "mostly
      // real" and be safe to trade on.
      simulated: false,
      banner: null,
      tradingDate: null,
      note: null,
    };
  },

  async coverage() {
    const rows = await query<Raw>(COVERAGE_SQL);
    return rows[0] ? mapCoverage(rows[0]) : null;
  },

  async workerStatus() {
    return (await query<Raw>(WORKER_SQL)).map(mapWorker);
  },

  async currentWatchlist(tradingDate) {
    return (await query<Raw>(WATCHLIST_SQL, [dateParam(tradingDate), null])).map(mapWatchlist);
  },

  async signals(filter: SignalFilter = {}) {
    return (
      await query<Raw>(SIGNALS_SQL, [
        dateParam(filter.tradingDate),
        filter.signalType ?? null,
        filter.symbol ?? null,
        limit(filter.limit, 500),
      ])
    ).map(mapSignal);
  },

  async history(limitPerDay = 30) {
    return (await query<Raw>(HISTORY_SQL, [limit(limitPerDay, 365)])).map(mapHistory);
  },

  async config() {
    const rows = await query<Raw>(CONFIG_SQL);
    return rows[0] ? mapConfig(rows[0]) : null;
  },

  async initialScan() {
    const rows = await query<Raw>(INITIAL_SCAN_SQL);
    const row = rows[0];
    if (!row) return null;
    const value = jsonRecord(row.new_value) as InitialScanSummary & Record<string, unknown>;
    return { ...value, completed_at: toIst(row.created_at as string | null) };
  },

  async instrument(symbol: string): Promise<InstrumentDetail | null> {
    const rows = await query<Raw>(INSTRUMENT_SQL, [symbol]);
    const instrument = rows[0] ? mapInstrument(rows[0]) : null;
    if (!instrument) return null;
    const candles = (await query<Raw>(CANDLES_SQL, [instrument.id, 400])).map(mapCandle).reverse();
    const snapshots = (await query<Raw>(SNAPSHOTS_SQL, [instrument.id, 400])).map(mapSnapshot).reverse();
    const events = (await query<Raw>(EVENTS_SQL, [null, instrument.id, 200])).map(mapEvent);
    const membership = (await query<Raw>(WATCHLIST_SQL, [null, instrument.symbol]).then((r) => r.map(mapWatchlist)))[0] ?? null;
    return { instrument, candles, snapshots, events, membership };
  },

  async deliveries(signalIds: number[]) {
    const ids = signalIds.map((id) => Math.trunc(id)).filter((id) => Number.isFinite(id));
    if (ids.length === 0) return [];
    return (await query<Raw>(DELIVERIES_SQL, [ids])).map(mapDelivery);
  },

  async sectorQuotes() {
    return (await query<Raw>(SECTOR_SQL)).map(mapSectorQuote);
  },
};
