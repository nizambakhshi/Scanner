/**
 * Row mappers shared by both data sources.
 *
 * A row is normalised exactly once, on its way out of the data layer: numbers
 * are numbers (PostgreSQL sends `NUMERIC` as a string and `COUNT(*)` as int8),
 * timestamps are Asia/Kolkata ISO strings, arrays are arrays, and `null` stays
 * `null` rather than becoming `0`. Both the SQL source and the replay source run
 * their rows through these functions, so a component cannot tell where a row came
 * from by looking at its types — which is the point of the interface in `types.ts`.
 */

import { bool, int, jsonRecord, num, strings, toIst } from "./format";
import type {
  CandleRow,
  ConfigRow,
  CoverageRow,
  DailyRsiPoint,
  DeliveryRow,
  EventRow,
  HistoryRow,
  InstrumentRow,
  RsiBias,
  SignalRow,
  SectorQuoteRow,
  SnapshotRow,
  StockOverviewRow,
  WatchlistRow,
  WeeklyRsiPoint,
  WorkerStatusRow,
} from "./types";
export type Raw = Record<string, unknown>;

const text = (value: unknown): string | null => (value === null || value === undefined ? null : String(value));
const time = (value: unknown): string | null => toIst(value as string | Date | null);
const optionalInt = (value: unknown): number | null => (value === null || value === undefined ? null : int(value));

const BIAS_VALUES = new Set(["BULLISH", "BEARISH", "NEUTRAL"]);
/** The stored classification, or null. Anything unexpected (a stray string, a
 *  half-written row) must degrade to "not classified" — the UI turns a non-null
 *  bias into a coloured claim about the stock, so it may only accept the three
 *  the sync is allowed to write. */
const bias = (value: unknown): RsiBias => {
  const raw = value === null || value === undefined ? "" : String(value);
  return BIAS_VALUES.has(raw) ? (raw as RsiBias) : null;
};

export function mapCoverage(row: Raw): CoverageRow {
  return {
    nifty500_count: int(row.nifty500_count),
    mapped_count: int(row.mapped_count),
    unmapped_count: int(row.unmapped_count),
    ambiguous_count: int(row.ambiguous_count),
    invalid_count: int(row.invalid_count),
    active_universe_count: int(row.active_universe_count),
    fno_count: int(row.fno_count),
    last_verified_at: time(row.last_verified_at),
  };
}

export function mapWorker(row: Raw): WorkerStatusRow {
  return {
    worker_id: text(row.worker_id) ?? "",
    status: text(row.status) ?? "",
    feed_status: text(row.feed_status) ?? "",
    last_market_data_at: time(row.last_market_data_at),
    last_completed_candle_at: time(row.last_completed_candle_at),
    subscribed_instrument_count: int(row.subscribed_instrument_count),
    monitored_instrument_count: int(row.monitored_instrument_count),
    universe_instrument_count: int(row.universe_instrument_count),
    mapped_instrument_count: int(row.mapped_instrument_count),
    active_watchlist_count: int(row.active_watchlist_count),
    pending_notification_count: int(row.pending_notification_count),
    queue_depth: int(row.queue_depth),
    processing_latency_ms: num(row.processing_latency_ms),
    last_error: text(row.last_error),
    scan_mode: text(row.scan_mode) ?? "live",
    simulation_label: bool(row.simulation_label),
    started_at: time(row.started_at),
    updated_at: time(row.updated_at),
  };
}

export function mapWatchlist(row: Raw): WatchlistRow {
  return {
    watchlist_id: int(row.watchlist_id),
    trading_session_id: int(row.trading_session_id),
    trading_date: text(row.trading_date) ?? "",
    instrument_id: int(row.instrument_id),
    symbol: text(row.symbol),
    trading_symbol: text(row.trading_symbol),
    company_name: text(row.company_name),
    exchange: text(row.exchange),
    current_state: String(row.current_state) as WatchlistRow["current_state"],
    initial_entry_at: time(row.initial_entry_at),
    latest_state_change_at: time(row.latest_state_change_at),
    initial_entry_price: num(row.initial_entry_price),
    latest_price: num(row.latest_price),
    latest_score: num(row.latest_score),
    latest_rank: optionalInt(row.latest_rank),
    removal_reason: text(row.removal_reason),
    consecutive_weak_candles: int(row.consecutive_weak_candles),
    episode_count: int(row.episode_count),
    is_active: bool(row.is_active),
    is_simulated: bool(row.is_simulated),
    vwap: num(row.vwap),
    ema9: num(row.ema9),
    ema20: num(row.ema20),
    relative_volume: num(row.relative_volume),
    intraday_change_pct: num(row.intraday_change_pct),
    distance_from_vwap_pct: num(row.distance_from_vwap_pct),
    ema_spread_pct: num(row.ema_spread_pct),
    session_high: num(row.session_high),
    session_low: num(row.session_low),
    distance_from_high_pct: num(row.distance_from_high_pct),
    bullish_qualified: bool(row.bullish_qualified),
    reasons: strings(row.reasons),
    failed_reasons: strings(row.failed_reasons),
    data_quality: text(row.data_quality),
    warming_up: bool(row.warming_up),
    latest_candle_at: time(row.latest_candle_at),
    indicator_snapshot_id: optionalInt(row.indicator_snapshot_id),
    daily_rsi: num(row.daily_rsi),
    weekly_rsi: num(row.weekly_rsi),
    rsi_bias: bias(row.rsi_bias),
  };
}

export function mapSignal(row: Raw): SignalRow {
  return {
    id: int(row.id),
    signal_type: String(row.signal_type) as SignalRow["signal_type"],
    event_timestamp: time(row.event_timestamp) ?? "",
    candle_start_at: time(row.candle_start_at),
    price: num(row.price),
    previous_close: num(row.previous_close),
    intraday_change_pct: num(row.intraday_change_pct),
    vwap: num(row.vwap),
    ema9: num(row.ema9),
    ema20: num(row.ema20),
    relative_volume: num(row.relative_volume),
    score: num(row.score),
    rank: optionalInt(row.rank),
    reasons: strings(row.reasons),
    message: text(row.message),
    config_version: int(row.config_version, 1),
    is_simulated: bool(row.is_simulated),
    trading_session_id: optionalInt(row.trading_session_id),
    trading_date: text(row.trading_date),
    instrument_id: optionalInt(row.instrument_id),
    symbol: text(row.symbol),
    trading_symbol: text(row.trading_symbol),
    company_name: text(row.company_name),
    exchange: text(row.exchange),
    telegram_delivered: bool(row.telegram_delivered),
    telegram_failed: bool(row.telegram_failed),
    telegram_status: text(row.telegram_status),
    telegram_attempts: int(row.telegram_attempts),
    telegram_error: text(row.telegram_error),
  };
}

export function mapHistory(row: Raw): HistoryRow {
  return {
    trading_session_id: int(row.trading_session_id),
    trading_date: text(row.trading_date) ?? "",
    session_status: text(row.session_status) ?? "",
    session_open_at: time(row.session_open_at) ?? "",
    session_close_at: time(row.session_close_at) ?? "",
    initial_scan_completed_at: time(row.initial_scan_completed_at),
    finalized_at: time(row.finalized_at),
    stale_data_seconds: int(row.stale_data_seconds),
    universe_count: int(row.universe_count),
    mapped_count: int(row.mapped_count),
    subscribed_count: int(row.subscribed_count),
    monitored_count: int(row.monitored_count),
    qualified_count: int(row.qualified_count),
    signals_generated: int(row.signals_generated),
    telegram_delivered: int(row.telegram_delivered),
    telegram_failed: int(row.telegram_failed),
    notes: text(row.notes),
  };
}

export function mapConfig(row: Raw): ConfigRow {
  const weights = jsonRecord(row.rank_weights);
  const rankWeights: Record<string, number> = {};
  for (const [key, value] of Object.entries(weights)) rankWeights[key] = num(value) ?? 0;
  return {
    id: int(row.id),
    name: text(row.name) ?? "default",
    is_enabled: bool(row.is_enabled),
    config_version: int(row.config_version, 1),
    universe: text(row.universe) ?? "NIFTY500",
    fast_ema_period: int(row.fast_ema_period),
    slow_ema_period: int(row.slow_ema_period),
    volume_lookback: int(row.volume_lookback),
    relative_volume_threshold: num(row.relative_volume_threshold) ?? 0,
    alert_cooldown_minutes: int(row.alert_cooldown_minutes),
    removal_confirm_candles: int(row.removal_confirm_candles),
    feed_staleness_threshold_seconds: int(row.feed_staleness_threshold_seconds),
    candle_grace_seconds: int(row.candle_grace_seconds),
    initial_scan_candles: int(row.initial_scan_candles),
    max_subscribed_instruments: int(row.max_subscribed_instruments),
    rank_weights: rankWeights,
  };
}

export function mapCandle(row: Raw): CandleRow {
  return {
    instrument_id: int(row.instrument_id),
    trading_date: text(row.trading_date) ?? "",
    candle_start_at: time(row.candle_start_at) ?? "",
    open: num(row.open) ?? 0,
    high: num(row.high) ?? 0,
    low: num(row.low) ?? 0,
    close: num(row.close) ?? 0,
    volume: int(row.volume),
    is_complete: bool(row.is_complete),
    data_quality_status: text(row.data_quality_status) ?? "",
    source_timestamp: time(row.source_timestamp),
    tick_count: int(row.tick_count),
    is_simulated: bool(row.is_simulated),
  };
}

export function mapSnapshot(row: Raw): SnapshotRow {
  return {
    id: int(row.id),
    instrument_id: int(row.instrument_id),
    trading_date: text(row.trading_date) ?? "",
    candle_start_at: time(row.candle_start_at) ?? "",
    close: num(row.close),
    previous_close: num(row.previous_close),
    vwap: num(row.vwap),
    ema9: num(row.ema9),
    ema20: num(row.ema20),
    average_volume20: num(row.average_volume20),
    relative_volume: num(row.relative_volume),
    intraday_change_pct: num(row.intraday_change_pct),
    distance_from_vwap_pct: num(row.distance_from_vwap_pct),
    ema_spread_pct: num(row.ema_spread_pct),
    session_high: num(row.session_high),
    session_low: num(row.session_low),
    distance_from_high_pct: num(row.distance_from_high_pct),
    bullish_qualified: bool(row.bullish_qualified),
    reasons: strings(row.reasons),
    failed_reasons: strings(row.failed_reasons),
    data_quality_status: text(row.data_quality_status) ?? "",
    warming_up: bool(row.warming_up),
    score: num(row.score),
    rank: optionalInt(row.rank),
    config_version: int(row.config_version, 1),
    is_simulated: bool(row.is_simulated),
  };
}

export function mapEvent(row: Raw): EventRow {
  return {
    id: int(row.id),
    watchlist_id: int(row.watchlist_id),
    symbol: text(row.symbol),
    event_type: text(row.event_type) ?? "",
    previous_state: text(row.previous_state),
    new_state: text(row.new_state) ?? "",
    event_timestamp: time(row.event_timestamp) ?? "",
    reason: text(row.reason),
    price: num(row.price),
    indicator_snapshot_id: optionalInt(row.indicator_snapshot_id),
    detail: jsonRecord(row.detail),
  };
}

export function mapDelivery(row: Raw): DeliveryRow {
  return {
    id: int(row.id),
    signal_id: int(row.signal_id),
    channel: String(row.channel) as DeliveryRow["channel"],
    delivery_status: text(row.delivery_status) ?? "",
    attempt_count: int(row.attempt_count),
    next_retry_at: time(row.next_retry_at),
    last_attempt_at: time(row.last_attempt_at),
    delivered_at: time(row.delivered_at),
    provider_message_id: text(row.provider_message_id),
    last_error: text(row.last_error),
    is_simulated: bool(row.is_simulated),
  };
}

export function mapInstrument(row: Raw): InstrumentRow {
  return {
    id: int(row.id),
    exchange: text(row.exchange) ?? "",
    symbol: text(row.symbol) ?? "",
    trading_symbol: text(row.trading_symbol) ?? "",
    company_name: text(row.company_name),
    instrument_type: text(row.instrument_type) ?? "",
    is_nifty500: bool(row.is_nifty500),
    is_active: bool(row.is_active),
    mapping_status: text(row.mapping_status) ?? "",
    previous_close: num(row.previous_close),
    last_verified_at: time(row.last_verified_at),
  };
}

export function mapSectorQuote(row: Raw): SectorQuoteRow {
  return {
    index_symbol: text(row.index_symbol) ?? "",
    display_name: text(row.display_name) ?? "",
    display_order: int(row.display_order),
    last_price: num(row.last_price),
    previous_close: num(row.previous_close),
    change_pct: num(row.change_pct),
    source_updated_at: time(row.source_updated_at),
  };
}

export function mapStockOverview(row: Raw): StockOverviewRow {
  return {
    instrument_id: int(row.instrument_id),
    symbol: text(row.symbol) ?? "",
    trading_symbol: text(row.trading_symbol) ?? "",
    company_name: text(row.company_name),
    exchange: text(row.exchange) ?? "",
    last_bar_date: text(row.last_bar_date),
    close: num(row.close),
    previous_close: num(row.previous_close),
    pe_ratio: num(row.pe_ratio),
    market_cap_cr: num(row.market_cap_cr),
    listing_date: text(row.listing_date),
    profile_source: text(row.profile_source),
    high_52w: num(row.high_52w),
    low_52w: num(row.low_52w),
    ath: num(row.ath),
    atl: num(row.atl),
    data_start: text(row.data_start),
    daily_rsi: num(row.daily_rsi),
    weekly_rsi: num(row.weekly_rsi),
    rsi_bias: bias(row.rsi_bias),
    rsi_as_of: text(row.rsi_as_of),
  };
}

/** The SQL filters `rsi14 IS NOT NULL`, so the point is always a real reading;
 *  the `?? 0` is the same required-number default the candle mapper uses. */
export function mapDailyRsiPoint(row: Raw): DailyRsiPoint {
  return {
    trading_date: text(row.trading_date) ?? "",
    close: num(row.close) ?? 0,
    rsi14: num(row.rsi14) ?? 0,
  };
}

export function mapWeeklyRsiPoint(row: Raw): WeeklyRsiPoint {
  return {
    week_start: text(row.week_start) ?? "",
    close: num(row.close) ?? 0,
    rsi14: num(row.rsi14) ?? 0,
  };
}

/** A `YYYY-MM-DD` filter, or null for "no filter". A malformed date must become
 *  NULL rather than reach the database as an invalid cast. */
export function dateParam(value?: string | null): string | null {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}
