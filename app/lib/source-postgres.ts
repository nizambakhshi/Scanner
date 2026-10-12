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
  mapDailyRsiPoint,
  mapDelivery,
  mapEvent,
  mapHistory,
  mapInstrument,
  mapOrbRow,
  mapSectorQuote,
  mapSignal,
  mapSnapshot,
  mapStockOverview,
  mapWatchlist,
  mapWeeklyRsiPoint,
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

/** Qualifiers first, then by score — the same order the watchlist ranks in (§11.1).
 *  The LEFT JOIN carries the two-timeframe RSI context; a stock whose instrument
 *  has no `stock_details` row yet renders the RSI cells as "—", never as 0. */
const WATCHLIST_SQL = `
  SELECT w.watchlist_id::int AS watchlist_id, w.trading_session_id::int AS trading_session_id,
         w.trading_date::text AS trading_date, w.instrument_id::int AS instrument_id,
         w.symbol, w.trading_symbol, w.company_name, w.exchange, w.current_state,
         w.initial_entry_at, w.latest_state_change_at,
         w.vwap::float8 AS vwap, w.ema9::float8 AS ema9, w.ema20::float8 AS ema20,
         w.relative_volume::float8 AS relative_volume,
         w.intraday_change_pct::float8 AS intraday_change_pct,
         w.distance_from_vwap_pct::float8 AS distance_from_vwap_pct,
         w.ema_spread_pct::float8 AS ema_spread_pct,
         w.session_high::float8 AS session_high, w.session_low::float8 AS session_low,
         w.distance_from_high_pct::float8 AS distance_from_high_pct,
         w.initial_entry_price::float8 AS initial_entry_price,
         w.latest_price::float8 AS latest_price, w.latest_score::float8 AS latest_score,
         w.latest_rank::int AS latest_rank, w.removal_reason,
         w.consecutive_weak_candles::int AS consecutive_weak_candles,
         w.episode_count::int AS episode_count,
         w.is_active, w.is_simulated, w.bullish_qualified, w.reasons, w.failed_reasons,
         w.data_quality, w.warming_up, w.latest_candle_at,
         w.indicator_snapshot_id::int AS indicator_snapshot_id,
         sd.daily_rsi::float8  AS daily_rsi,
         sd.weekly_rsi::float8 AS weekly_rsi,
         sd.rsi_bias
  FROM v_current_watchlist w
  LEFT JOIN stock_details sd ON sd.instrument_id = w.instrument_id
  WHERE ($1::date IS NULL OR w.trading_date = $1::date)
    AND ($2::text IS NULL OR upper(w.symbol) = upper($2::text))
  ORDER BY (w.current_state IN ('ACTIVE','REQUALIFIED')) DESC,
           w.latest_score DESC NULLS LAST,
           w.symbol`;

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

/** The /stocks directory — the worker's universe LEFT JOINed to its newest daily
 *  bar, the fundamentals and the two-timeframe RSI. Which instruments count as
 *  "the universe" follows the newest saved config, the same `is_fno` /
 *  `is_nifty500` switch the worker applies, so the page lists exactly what the
 *  scanner scans. The laterals keep instruments without a synced daily bar in
 *  the list (they render dashes, never zeros); `$1` narrows to one symbol for
 *  the detail page, which already knows the instrument. */
const STOCK_OVERVIEW_SQL = `
  WITH cfg AS (
    SELECT COALESCE(
      (SELECT universe FROM scanner_configs
       ORDER BY is_enabled DESC, config_version DESC LIMIT 1), 'FNO') AS universe
  )
  SELECT i.id::int AS instrument_id, i.symbol, i.trading_symbol, i.company_name, i.exchange,
         latest.trading_date::text AS last_bar_date,
         latest.close::float8  AS close,
         prev.close::float8    AS previous_close,
         sd.pe_ratio::float8      AS pe_ratio,
         sd.market_cap_cr::float8 AS market_cap_cr,
         sd.listing_date::text    AS listing_date,
         sd.profile_source,
         sd.high_52w::float8 AS high_52w, sd.low_52w::float8 AS low_52w,
         sd.ath::float8 AS ath, sd.atl::float8 AS atl,
         sd.data_start::text AS data_start,
         sd.daily_rsi::float8  AS daily_rsi,
         sd.weekly_rsi::float8 AS weekly_rsi,
         sd.rsi_bias, sd.rsi_as_of::text AS rsi_as_of
  FROM instruments i
  CROSS JOIN cfg
  LEFT JOIN stock_details sd ON sd.instrument_id = i.id
  LEFT JOIN LATERAL (
    SELECT c.trading_date, c.close FROM candles_1d c
    WHERE c.instrument_id = i.id
    ORDER BY c.trading_date DESC
    LIMIT 1
  ) latest ON true
  LEFT JOIN LATERAL (
    SELECT c.close FROM candles_1d c
    WHERE c.instrument_id = i.id AND c.trading_date < latest.trading_date
    ORDER BY c.trading_date DESC
    LIMIT 1
  ) prev ON true
  WHERE i.is_active AND i.mapping_status = 'MAPPED'
    AND (cfg.universe <> 'FNO' OR i.is_fno)
    AND (cfg.universe <> 'NIFTY500' OR i.is_nifty500)
    AND ($1::text IS NULL OR upper(i.symbol) = upper($1::text)
                          OR upper(i.trading_symbol) = upper($1::text))
  ORDER BY i.symbol`;

/** The last N synced daily RSI readings, newest first; callers reverse. Only
 *  bars with a defined RSI-14 (history ≥ 14) are served — a missing reading is
 *  dropped, not filled with a placeholder. */
const DAILY_RSI_SQL = `
  SELECT trading_date::text AS trading_date, close::float8 AS close, rsi14::float8 AS rsi14
  FROM candles_1d
  WHERE instrument_id = $1::int AND rsi14 IS NOT NULL
  ORDER BY trading_date DESC
  LIMIT $2::int`;

const WEEKLY_RSI_SQL = `
  SELECT week_start::text AS week_start, close::float8 AS close, rsi14::float8 AS rsi14
  FROM candles_1w
  WHERE instrument_id = $1::int AND rsi14 IS NOT NULL
  ORDER BY week_start DESC
  LIMIT $2::int`;

/**
 * The /strategy ORB board: for every instrument in the configured universe, the
 * newest session's opening range and first 5-minute breakout, computed from the
 * stored 1-minute candles only.
 *
 * Session window: the newest `trading_date` in candles_1m, 9:15–15:30 IST. The
 * series is cut into 5-minute buckets by wall-clock offset from the open
 * (`bucket` 0 IS the opening range), so a bucket close is the last 1-minute
 * close in it — exactly the "5-minute close" the strategy trades on. The first
 * bucket after the range whose close is beyond the range high/low is the
 * breakout; `*_ratio` is that bucket's volume over the mean of every 5-minute
 * bucket before it (the range candle included — the volume evidence available
 * at breakout time). `session_vwap` is Σ(TP·V)/ΣV over the same bars, the
 * worker's own VWAP formula, so the two pages can never disagree. EMA 9/20 is
 * the only field not derivable from one session: it is the worker's streaming
 * EMA carried across sessions, read from the newest snapshot of the session
 * (null until the worker has scanned the symbol). No broker token leaves the
 * database (§19).
 */
const ORB_STRATEGY_SQL = `
  WITH cfg AS (
    SELECT COALESCE(
      (SELECT universe FROM scanner_configs
       ORDER BY is_enabled DESC, config_version DESC LIMIT 1), 'FNO') AS universe
  ),
  w AS (
    SELECT d.trading_date,
           (d.trading_date::timestamp + time '09:15') AT TIME ZONE 'Asia/Kolkata' AS open_at,
           (d.trading_date::timestamp + time '15:30') AT TIME ZONE 'Asia/Kolkata' AS close_at
    FROM (SELECT max(trading_date) AS trading_date FROM candles_1m) d
  ),
  u AS (
    SELECT i.id AS instrument_id, i.symbol, i.trading_symbol, i.company_name
    FROM instruments i CROSS JOIN cfg
    WHERE i.is_active AND i.mapping_status = 'MAPPED'
      AND (cfg.universe <> 'FNO' OR i.is_fno)
      AND (cfg.universe <> 'NIFTY500' OR i.is_nifty500)
  ),
  bars AS (
    SELECT c.instrument_id, c.candle_start_at,
           floor(extract(epoch FROM (c.candle_start_at - w.open_at)) / 300.0)::int AS bucket,
           c.high, c.low, c.close, c.volume
    FROM candles_1m c CROSS JOIN w
    WHERE c.trading_date = w.trading_date
      AND c.candle_start_at >= w.open_at
      AND c.candle_start_at < w.close_at
  ),
  bucket AS (
    SELECT instrument_id, bucket,
           max(candle_start_at) AS last_at,
           max(high) AS high, min(low) AS low,
           (array_agg(close ORDER BY candle_start_at DESC))[1] AS close,
           sum(volume) AS volume,
           sum(volume * (high + low + close) / 3.0) AS price_volume,
           count(*) AS bar_count
    FROM bars
    GROUP BY instrument_id, bucket
  ),
  agg AS (
    SELECT b.*,
           sum(volume) OVER before_now AS volume_before,
           count(*)    OVER before_now AS buckets_before,
           sum(volume) OVER through_now AS volume_through,
           sum(price_volume) OVER through_now AS price_volume_through
    FROM bucket b
    WINDOW before_now AS (PARTITION BY instrument_id ORDER BY bucket
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
           through_now AS (PARTITION BY instrument_id ORDER BY bucket
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
  ),
  orng AS (
    SELECT instrument_id, high AS or_high, low AS or_low, bar_count AS or_bars
    FROM agg WHERE bucket = 0
  ),
  bu AS (
    SELECT DISTINCT ON (a.instrument_id)
           a.instrument_id, a.last_at, a.close, a.volume,
           a.volume::numeric / nullif(a.volume_before::numeric / nullif(a.buckets_before, 0), 0) AS ratio
    FROM agg a JOIN orng o USING (instrument_id)
    WHERE a.bucket >= 1 AND a.close > o.or_high
    ORDER BY a.instrument_id, a.bucket
  ),
  bd AS (
    SELECT DISTINCT ON (a.instrument_id)
           a.instrument_id, a.last_at, a.close, a.volume,
           a.volume::numeric / nullif(a.volume_before::numeric / nullif(a.buckets_before, 0), 0) AS ratio
    FROM agg a JOIN orng o USING (instrument_id)
    WHERE a.bucket >= 1 AND a.close < o.or_low
    ORDER BY a.instrument_id, a.bucket
  ),
  latest AS (
    SELECT DISTINCT ON (instrument_id)
           instrument_id, last_at, close, volume_through, price_volume_through
    FROM agg ORDER BY instrument_id, bucket DESC
  ),
  snap AS (
    SELECT DISTINCT ON (s.instrument_id)
           s.instrument_id, s.candle_start_at AS snap_at, s.ema9, s.ema20
    FROM indicator_snapshots s CROSS JOIN w
    WHERE s.trading_date = w.trading_date
    ORDER BY s.instrument_id, s.candle_start_at DESC
  )
  SELECT u.instrument_id::int AS instrument_id, u.symbol, u.trading_symbol, u.company_name,
         (SELECT w.trading_date::text FROM w) AS session_date,
         o.or_high::float8 AS or_high, o.or_low::float8 AS or_low, o.or_bars::int AS or_bars,
         bu.last_at AS bu_at, bu.close::float8 AS bu_close, bu.volume::int8 AS bu_volume,
         bu.ratio::float8 AS bu_ratio,
         bd.last_at AS bd_at, bd.close::float8 AS bd_close, bd.volume::int8 AS bd_volume,
         bd.ratio::float8 AS bd_ratio,
         lt.last_at AS latest_at, lt.close::float8 AS latest_close,
         (lt.price_volume_through / nullif(lt.volume_through, 0))::float8 AS session_vwap,
         sn.snap_at, sn.ema9::float8 AS ema9, sn.ema20::float8 AS ema20
  FROM u
  LEFT JOIN orng o USING (instrument_id)
  LEFT JOIN bu USING (instrument_id)
  LEFT JOIN bd USING (instrument_id)
  LEFT JOIN latest lt USING (instrument_id)
  LEFT JOIN snap sn USING (instrument_id)
  ORDER BY u.symbol`;

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
    const overviewRows = await query<Raw>(STOCK_OVERVIEW_SQL, [instrument.symbol]);
    const overview = overviewRows[0] ? mapStockOverview(overviewRows[0]) : null;
    // Newest-first in SQL, oldest-first for rendering — a chart axis runs left to right.
    const daily_rsi = (await query<Raw>(DAILY_RSI_SQL, [instrument.id, 7])).map(mapDailyRsiPoint).reverse();
    const weekly_rsi = (await query<Raw>(WEEKLY_RSI_SQL, [instrument.id, 7])).map(mapWeeklyRsiPoint).reverse();
    return { instrument, candles, snapshots, events, membership, overview, daily_rsi, weekly_rsi };
  },

  async stocksOverview() {
    return (await query<Raw>(STOCK_OVERVIEW_SQL, [null])).map(mapStockOverview);
  },

  async orbStrategy() {
    return (await query<Raw>(ORB_STRATEGY_SQL)).map(mapOrbRow);
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
