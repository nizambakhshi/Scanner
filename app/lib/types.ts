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
  /** F&O underlyings flagged in the instruments table — the universe the
   *  worker actually scans when configured for FNO. 0 in replay data. */
  fno_count: number;
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
  /** Two-timeframe RSI context from `stock_details` (LEFT JOIN, nullable until
   *  scanner-sync-daily has run for the instrument). */
  daily_rsi: number | null;
  weekly_rsi: number | null;
  rsi_bias: RsiBias;
  /** Today's five-minute RSI and the side it puts this stock on, read from the
   *  same snapshot row the VWAP and EMA columns come from — the board's number and
   *  the detector's number are one stored value. `rsi_5m` is null until the session
   *  has printed fifteen buckets; `rsi_state` is null on every candle stored before
   *  migration 0011 ran. */
  rsi_5m: number | null;
  rsi_state: RsiBias;
};

/** The three sides of the operator's RSI rule, as of 2026-10-15:
 *  **ENTRY** — RSI below 40, the green side, the dip to act on.
 *  **OVERBOUGHT** — RSI above 60, the red side.
 *  **NEUTRAL** — the 40–60 band, or an extreme the other half of the rule rejects.
 *  `null` = not measured yet (never silently NEUTRAL).
 *
 *  Both horizons spell the same three words: the nightly daily+weekly pair in
 *  `stock_details.rsi_bias`, and today's five-minute reading judged against the live
 *  trend in `indicator_snapshots.rsi_state`. The old BULLISH/BEARISH pair described
 *  momentum and pointed the colours the wrong way; migration 0011 translated the
 *  stored rows rather than leaving a word that meant one thing here and another
 *  there. */
export type RsiBias = "ENTRY" | "OVERBOUGHT" | "NEUTRAL" | null;

/**
 * One row of the /stocks directory: the instrument, its latest daily bar
 * (the "Price" and daily "Change %"), the fundamentals scanner-sync-profiles
 * fetched, and the technicals scanner-sync-daily computed. Nulls mean "not
 * synced yet" and render as dashes — never 0, never an implied value.
 */
export type StockOverviewRow = {
  instrument_id: number;
  symbol: string;
  trading_symbol: string;
  company_name: string | null;
  exchange: string;
  /** Newest daily bar (the latest completed session). */
  last_bar_date: string | null;
  close: number | null;
  previous_close: number | null;
  pe_ratio: number | null;
  market_cap_cr: number | null;
  listing_date: string | null;
  profile_source: string | null;
  high_52w: number | null;
  low_52w: number | null;
  ath: number | null;
  atl: number | null;
  data_start: string | null;
  daily_rsi: number | null;
  weekly_rsi: number | null;
  rsi_bias: RsiBias;
  rsi_as_of: string | null;
};

/** One point of the per-instrument RSI history (candles_1d.rsi14). */
export type DailyRsiPoint = {
  trading_date: string;
  close: number;
  rsi14: number;
};

/** One point of the per-instrument weekly RSI history (candles_1w.rsi14). */
export type WeeklyRsiPoint = {
  week_start: string;
  close: number;
  rsi14: number;
};

/**
 * One row of the /strategy ORB board, for one instrument and the newest trading
 * session that has 1-minute data: the opening range (the 9:15–9:20 IST five
 * minutes), the first 5-minute candle whose *close* left that range, the
 * confirmation readings, and the newest 5-minute close.
 *
 * Every field is computed from `candles_1m` except the EMA pair, which comes
 * from the newest `indicator_snapshots` row of the session (the worker's
 * streaming EMA carries across sessions and cannot be replayed from one
 * session's bars). Nulls mean "the session has not produced this yet" — a
 * breakout that has not happened is null, never a placeholder.
 */
export type OrbRow = {
  instrument_id: number;
  symbol: string;
  trading_symbol: string;
  company_name: string | null;
  /** The session this board describes (YYYY-MM-DD, IST). Null on an empty DB. */
  session_date: string | null;
  or_high: number | null;
  or_low: number | null;
  /** 1-minute bars the range is built from — 5 is a full range, fewer is thin. */
  or_bars: number | null;
  /** First 5-minute close above the range high (after 9:20). */
  bu_at: string | null;
  bu_close: number | null;
  bu_volume: number | null;
  /** Breakout-candle volume ÷ average 5-minute volume before it (null: no volume history). */
  bu_ratio: number | null;
  /** First 5-minute close below the range low (after 9:20). */
  bd_at: string | null;
  bd_close: number | null;
  bd_volume: number | null;
  bd_ratio: number | null;
  /** Newest 5-minute close of the session (may still be forming live). */
  latest_at: string | null;
  latest_close: number | null;
  /** Session VWAP over every 1-minute candle so far — Σ(TP·V)/ΣV, the worker's formula. */
  session_vwap: number | null;
  /** 1-minute EMA 9/20 from the newest snapshot; null when the worker has not scanned this symbol. */
  ema9: number | null;
  ema20: number | null;
  snap_at: string | null;
};

/** Which side of the range the first breakout left through. */
export type OrbDirection = "UP" | "DOWN";

export type OrbStatus = "NO_DATA" | "OPENING" | "INSIDE" | "BROKE_UP" | "BROKE_DOWN";

/**
 * The derived view the table renders — see `lib/orb.ts` for `deriveOrb`, which
 * is where the strategy arithmetic lives so it can be unit-tested without a
 * database. `null` on an agreement flag means "cannot be judged" (no breakout
 * direction yet, or no EMA/VWAP reading), which is deliberately distinct from
 * `false` ("judged, and it fails").
 */
export type OrbView = {
  row: OrbRow;
  status: OrbStatus;
  direction: OrbDirection | null;
  /** A breakout happened, but the newest close is back inside the range. */
  retreated: boolean;
  width: number | null;
  /** Range width as a percentage of the range low — comparable across price levels. */
  width_pct: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  vwap_side: "above" | "below" | null;
  vwap_agrees: boolean | null;
  volume_agrees: boolean | null;
  ema_agrees: boolean | null;
  /** A breakout whose three filter checks all pass — the strategy's full setup. */
  confirmed: boolean;
};

export type SignalType =
  | "BULLISH_ENTRY"
  | "WATCHLIST_ADDED"
  | "WATCHLIST_WEAKENING"
  | "WATCHLIST_REMOVED"
  | "WATCHLIST_REQUALIFIED"
  | "DATA_FEED_INTERRUPTED"
  | "DATA_FEED_RECOVERED"
  | "DAILY_SCAN_COMPLETE"
  /** Retired 2026-10-15: the two-timeframe momentum pair, whose words pointed the
   *  colours the wrong way. Rows written under it still read, so its types stay legal. */
  | "RSI_BULLISH"
  | "RSI_BEARISH"
  /** The two sides of the intraday entry rule, raised by the `rsi` detector:
   *  RSI-14 below 40 in an up-trend, above 60 in a down-trend. */
  | "RSI_ENTRY"
  | "RSI_OVERBOUGHT"
  /** The two level-break types. Direction is the news; which strategy saw it is
   *  carried in `reasons` as `strategy:<name>` — see /alerts. */
  | "BREAKOUT_UP"
  | "BREAKDOWN";

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
  /** Wilder RSI-14 on this session's five-minute closes, measured on the candle that
   *  closes a bucket — so most snapshot rows carry null by construction, not by
   *  accident. `rsi_state` is the side that reading put the stock on once the trend
   *  confirmed it. Both are null on candles stored before migration 0011. */
  rsi_5m: number | null;
  rsi_state: RsiBias;
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
  /** The `stock_details` row — RSI classification, 52W/ATH/ATL, profile. Null
   *  until the nightly syncs have run for this instrument. */
  overview: StockOverviewRow | null;
  /** Daily RSI readings, oldest → newest (the last ~7 sessions). */
  daily_rsi: DailyRsiPoint[];
  /** Weekly RSI readings, oldest → newest (the last ~8 ISO weeks). */
  weekly_rsi: WeeklyRsiPoint[];
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
  /** The trading day the dashboard shows: the IST calendar date right now.
   *  The watchlist and feed anchor on this exact date — rows from a finished
   *  session never appear as if they were today's. Null only if the source
   *  cannot name a day (the replay fixture always can: its meta date). */
  activeTradingDate(): Promise<string | null>;
  currentWatchlist(tradingDate?: string | null): Promise<WatchlistRow[]>;
  signals(filter?: SignalFilter): Promise<SignalRow[]>;
  history(limit?: number): Promise<HistoryRow[]>;
  config(): Promise<ConfigRow | null>;
  initialScan(): Promise<InitialScanSummary | null>;
  instrument(symbol: string): Promise<InstrumentDetail | null>;
  /** The /stocks directory: every instrument the worker scans, the newest
   *  daily bar, the fundamentals and the two-timeframe RSI. */
  stocksOverview(): Promise<StockOverviewRow[]>;
  /** The /strategy ORB board: the opening range, first breakout and
   *  confirmation readings for a single session — `tradingDate` when given,
   *  the newest session with candles otherwise. */
  orbStrategy(tradingDate?: string | null): Promise<OrbRow[]>;
  /** Table sparklines, `instrument_id → closes` oldest → newest. Intraday: the
   *  newest session's closes sampled per 5-minute bucket from `candles_1m`.
   *  A missing key means "no series" — the table renders a dash, never a
   *  fabricated line. */
  intradaySparklines(): Promise<Record<number, number[]>>;
  /** Table sparklines over the newest ~15 completed daily closes (`candles_1d`). */
  dailySparklines(): Promise<Record<number, number[]>>;
  deliveries(signalIds: number[]): Promise<DeliveryRow[]>;
  sectorQuotes(): Promise<SectorQuoteRow[]>;
  /** The symbols of the universe the scanner is configured to scan — the same
   *  F&O / NIFTY500 switch the worker applies. Used to tag news headlines;
   *  names only, no broker id leaves the database (§19). */
  universeSymbols(): Promise<string[]>;
}
