/**
 * Row shapes the dashboard consumes.
 *
 * These mirror the PostgreSQL views in `supabase/migrations/0003_views.sql`
 * column-for-column, in `snake_case`, because the database is the contract: the
 * SQL source selects those columns and the replay source emits the same names
 * (they are produced by `worker/worker/scripts/export_dashboard_fixture.py`,
 * which asserts against the migration). A rename in a migration therefore breaks
 * the exporter's assertion and the TypeScript type at the same time, rather than
 * silently rendering an empty cell.
 *
 * Every timestamp is an ISO-8601 string already converted to Asia/Kolkata, and
 * every numeric is a `number` — both sources normalise before returning, so no
 * component ever has to know that `NUMERIC` arrives as a string over the wire.
 */

export type DataSourceName = "postgres" | "replay" | "none";

export type SourceMeta = {
  source: DataSourceName;
  /** True for the replay sample. The UI must show a banner whenever this is true. */
  simulated: boolean;
  banner: string | null;
  tradingDate: string | null;
  note: string | null;
};

export type CoverageRow = {
  nifty500_count: number;
  mapped_count: number;
  unmapped_count: number;
  ambiguous_count: number;
  invalid_count: number;
  active_universe_count: number;
  last_verified_at: string | null;
};

export type WorkerStatusRow = {
  worker_id: string;
  status: string;
  feed_status: string;
  last_market_data_at: string | null;
  last_completed_candle_at: string | null;
  subscribed_instrument_count: number;
  monitored_instrument_count: number;
  universe_instrument_count: number;
  mapped_instrument_count: number;
  active_watchlist_count: number;
  pending_notification_count: number;
  queue_depth: number;
  processing_latency_ms: number | null;
  last_error: string | null;
  scan_mode: string;
  simulation_label: boolean;
  started_at: string | null;
  updated_at: string | null;
};

export type WatchlistState =
  | "WARMING_UP"
  | "ACTIVE"
  | "WEAKENING"
  | "REMOVED"
  | "REQUALIFIED"
  | "DATA_STALE";

export type WatchlistRow = {
  watchlist_id: number;
  trading_session_id: number;
  trading_date: string;
  instrument_id: number;
  symbol: string | null;
  trading_symbol: string | null;
  company_name: string | null;
  exchange: string | null;
  current_state: WatchlistState;
  initial_entry_at: string | null;
  latest_state_change_at: string | null;
  initial_entry_price: number | null;
  latest_price: number | null;
  latest_score: number | null;
  latest_rank: number | null;
  removal_reason: string | null;
  consecutive_weak_candles: number;
  episode_count: number;
  is_active: boolean;
  is_simulated: boolean;
  vwap: number | null;
  ema9: number | null;
  ema20: number | null;
  relative_volume: number | null;
  intraday_change_pct: number | null;
  distance_from_vwap_pct: number | null;
  ema_spread_pct: number | null;
  session_high: number | null;
  session_low: number | null;
  distance_from_high_pct: number | null;
  bullish_qualified: boolean;
  reasons: string[];
  failed_reasons: string[];
  data_quality: string | null;
  warming_up: boolean;
  latest_candle_at: string | null;
  indicator_snapshot_id: number | null;
};

export type SignalType =
  | "BULLISH_ENTRY"
  | "WATCHLIST_ADDED"
  | "WATCHLIST_WEAKENING"
  | "WATCHLIST_REMOVED"
  | "WATCHLIST_REQUALIFIED"
  | "DATA_FEED_INTERRUPTED"
  | "DATA_FEED_RECOVERED"
  | "DAILY_SCAN_COMPLETE";

export type SignalRow = {
  id: number;
  signal_type: SignalType;
  event_timestamp: string;
  candle_start_at: string | null;
  price: number | null;
  previous_close: number | null;
  intraday_change_pct: number | null;
  vwap: number | null;
  ema9: number | null;
  ema20: number | null;
  relative_volume: number | null;
  score: number | null;
  rank: number | null;
  reasons: string[];
  message: string | null;
  config_version: number;
  is_simulated: boolean;
  trading_session_id: number | null;
  trading_date: string | null;
  instrument_id: number | null;
  symbol: string | null;
  trading_symbol: string | null;
  company_name: string | null;
  exchange: string | null;
  telegram_delivered: boolean;
  telegram_failed: boolean;
  telegram_status: string | null;
  telegram_attempts: number;
  telegram_error: string | null;
};

export type HistoryRow = {
  trading_session_id: number;
  trading_date: string;
  session_status: string;
  session_open_at: string;
  session_close_at: string;
  initial_scan_completed_at: string | null;
  finalized_at: string | null;
  stale_data_seconds: number;
  universe_count: number;
  mapped_count: number;
  subscribed_count: number;
  monitored_count: number;
  qualified_count: number;
  signals_generated: number;
  telegram_delivered: number;
  telegram_failed: number;
  notes: string | null;
};

export type CandleRow = {
  instrument_id: number;
  trading_date: string;
  candle_start_at: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_complete: boolean;
  data_quality_status: string;
  source_timestamp: string | null;
  tick_count: number;
  is_simulated: boolean;
};

export type SnapshotRow = {
  id: number;
  instrument_id: number;
  trading_date: string;
  candle_start_at: string;
  close: number | null;
  previous_close: number | null;
  vwap: number | null;
  ema9: number | null;
  ema20: number | null;
  average_volume20: number | null;
  relative_volume: number | null;
  intraday_change_pct: number | null;
  distance_from_vwap_pct: number | null;
  ema_spread_pct: number | null;
  session_high: number | null;
  session_low: number | null;
  distance_from_high_pct: number | null;
  bullish_qualified: boolean;
  reasons: string[];
  failed_reasons: string[];
  data_quality_status: string;
  warming_up: boolean;
  score: number | null;
  rank: number | null;
  config_version: number;
  is_simulated: boolean;
};

export type EventRow = {
  id: number;
  watchlist_id: number;
  symbol: string | null;
  event_type: string;
  previous_state: string | null;
  new_state: string;
  event_timestamp: string;
  reason: string | null;
  price: number | null;
  indicator_snapshot_id: number | null;
  detail: Record<string, unknown>;
};

export type DeliveryRow = {
  id: number;
  signal_id: number;
  channel: "TELEGRAM" | "DASHBOARD";
  delivery_status: string;
  attempt_count: number;
  next_retry_at: string | null;
  last_attempt_at: string | null;
  delivered_at: string | null;
  provider_message_id: string | null;
  last_error: string | null;
  is_simulated: boolean;
};

/** Deliberately has no `instrument_token`: the browser never needs a broker id. */
export type InstrumentRow = {
  id: number;
  exchange: string;
  symbol: string;
  trading_symbol: string;
  company_name: string | null;
  instrument_type: string;
  is_nifty500: boolean;
  is_active: boolean;
  mapping_status: string;
  previous_close: number | null;
  last_verified_at: string | null;
};

/**
 * One chip in the sector tile: the fixed index row LEFT JOINed to its live
 * quote. A missing quote (`last_price === null`) means the worker has not
 * flushed yet — the tile shows "waiting", it never invents a price. No
 * `instrument_token` here either: the browser never needs a broker id.
 */
export type SectorQuoteRow = {
  index_symbol: string;
  display_name: string;
  display_order: number;
  last_price: number | null;
  previous_close: number | null;
  change_pct: number | null;
  source_updated_at: string | null;
};

export type ConfigRow = {
  id: number;
  name: string;
  is_enabled: boolean;
  config_version: number;
  universe: string;
  fast_ema_period: number;
  slow_ema_period: number;
  volume_lookback: number;
  relative_volume_threshold: number;
  alert_cooldown_minutes: number;
  removal_confirm_candles: number;
  feed_staleness_threshold_seconds: number;
  candle_grace_seconds: number;
  initial_scan_candles: number;
  max_subscribed_instruments: number;
  rank_weights: Record<string, number>;
};

/** The counts the pipeline journals into `audit_logs` at the initial scan. */
export type InitialScanSummary = {
  kind?: string;
  trading_date?: string;
  universe?: number;
  counts?: Record<string, number>;
  ranked?: string[];
  candles_required?: number;
  initial_scan_candles?: number;
  completed_at?: string | null;
  skipped?: string;
};

export type InstrumentDetail = {
  instrument: InstrumentRow | null;
  candles: CandleRow[];
  snapshots: SnapshotRow[];
  events: EventRow[];
  membership: WatchlistRow | null;
};

export type SignalFilter = {
  tradingDate?: string | null;
  signalType?: string | null;
  symbol?: string | null;
  limit?: number;
};

/**
 * Everything a page can ask for, implemented twice: once in SQL over the views,
 * once over the replay fixture. Keeping it an interface is what lets the health
 * page state, in one place, which numbers came from a real database.
 */
export interface ScannerSource {
  readonly name: DataSourceName;
  meta(): Promise<SourceMeta>;
  coverage(): Promise<CoverageRow | null>;
  workerStatus(): Promise<WorkerStatusRow[]>;
  currentWatchlist(tradingDate?: string | null): Promise<WatchlistRow[]>;
  signals(filter?: SignalFilter): Promise<SignalRow[]>;
  history(limit?: number): Promise<HistoryRow[]>;
  config(): Promise<ConfigRow | null>;
  initialScan(): Promise<InitialScanSummary | null>;
  instrument(symbol: string): Promise<InstrumentDetail | null>;
  deliveries(signalIds: number[]): Promise<DeliveryRow[]>;
  sectorQuotes(): Promise<SectorQuoteRow[]>;
}
