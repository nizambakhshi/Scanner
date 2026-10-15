import assert from "node:assert/strict";
import { test } from "node:test";
import { mapCandle, mapConfig, mapCoverage, mapDailyRsiPoint, mapDelivery, mapEvent, mapHistory, mapInstrument, mapSignal, mapSnapshot, mapStockOverview, mapWatchlist, mapWeeklyRsiPoint, mapWorker, dateParam } from "../lib/rows";
import { bool, int, jsonRecord, toIst } from "../lib/format";

test("dateParam refuses a malformed date instead of sending it to the database", () => {
  assert.equal(dateParam("2026-10-12"), "2026-10-12");
  assert.equal(dateParam("12/10/2026"), null);
  assert.equal(dateParam("2026-10-12'; DROP TABLE candles_1m;--"), null);
  assert.equal(dateParam(""), null);
  assert.equal(dateParam(undefined), null);
});

test("NUMERIC and int8 arrive as strings and must leave as numbers", () => {
  const coverage = mapCoverage({
    nifty500_count: "500",
    mapped_count: 480,
    unmapped_count: "12",
    ambiguous_count: null,
    invalid_count: "8",
    active_universe_count: "492",
    fno_count: "212",
    last_verified_at: new Date("2026-10-12T03:00:00Z"),
  });
  assert.equal(coverage.nifty500_count, 500);
  assert.equal(coverage.mapped_count, 480);
  assert.equal(coverage.ambiguous_count, 0);
  assert.equal(coverage.fno_count, 212);
  assert.ok(toIst("2026-10-12T03:00:00Z") === coverage.last_verified_at);
});

test("a null indicator stays null through the mapper", () => {
  const snapshot = mapSnapshot({
    id: 1,
    instrument_id: "41",
    trading_date: "2026-10-12",
    candle_start_at: new Date("2026-10-12T03:45:00Z"),
    close: "233.45",
    ema9: null,
    relative_volume: null,
    bullish_qualified: false,
    reasons: null,
    score: null,
    rank: null,
  });
  assert.equal(snapshot.close, 233.45);
  assert.equal(snapshot.ema9, null);
  assert.equal(snapshot.relative_volume, null);
  assert.equal(snapshot.score, null);
  assert.equal(snapshot.rank, null);
  assert.deepEqual(snapshot.reasons, []);
  assert.equal(snapshot.candle_start_at, "2026-10-12T09:15:00+05:30");
});

test("booleans come back in whatever form the driver felt like", () => {
  assert.equal(mapWatchlist({ current_state: "ACTIVE", is_active: "t", is_simulated: 1, warming_up: false }).is_active, true);
  assert.equal(mapWatchlist({ current_state: "ACTIVE", is_active: true }).is_simulated, false);
  assert.equal(bool("true"), true);
  assert.equal(int("x", 3), 3);
});

test("the signal row carries the delivery summary the view exposes", () => {
  const signal = mapSignal({
    id: "7",
    signal_type: "BULLISH_ENTRY",
    event_timestamp: new Date("2026-10-12T04:01:00Z"),
    price: "101.25",
    reasons: "{CLOSE_ABOVE_VWAP}",
    telegram_delivered: null,
    telegram_failed: null,
    telegram_attempts: null,
  });
  assert.equal(signal.id, 7);
  assert.equal(signal.price, 101.25);
  assert.deepEqual(signal.reasons, ["CLOSE_ABOVE_VWAP"]);
  // Absent delivery rows must not read as "delivered".
  assert.equal(signal.telegram_delivered, false);
  assert.equal(signal.telegram_attempts, 0);
});

test("worker rows keep the simulation label, because a sim heartbeat is not a live one", () => {
  const worker = mapWorker({ worker_id: "w1", simulation_label: true, processing_latency_ms: "12.7", queue_depth: "3" });
  assert.equal(worker.simulation_label, true);
  assert.equal(worker.processing_latency_ms, 12.7);
  assert.equal(worker.queue_depth, 3);
  assert.equal(worker.scan_mode, "live");
});

test("a delivery row is keyed to its signal so the table can join them in memory", () => {
  const delivery = mapDelivery({ id: 2, signal_id: "9", channel: "TELEGRAM", delivery_status: "FAILED", attempt_count: "3" });
  assert.equal(delivery.signal_id, 9);
  assert.equal(delivery.attempt_count, 3);
  assert.equal(delivery.channel, "TELEGRAM");
});

test("config weights survive a JSON round trip", () => {
  const config = mapConfig({
    id: 1,
    name: "default",
    rank_weights: JSON.stringify({ price_strength: 0.3, relative_volume: "0.3" }),
    relative_volume_threshold: "1.5000",
  });
  assert.equal(config.relative_volume_threshold, 1.5);
  assert.deepEqual(config.rank_weights, { price_strength: 0.3, relative_volume: 0.3 });
  assert.equal(jsonRecord(null)["anything"], undefined);
});

test("candle, event, history and instrument mappers normalise their own rows", () => {
  const candle = mapCandle({ instrument_id: 1, trading_date: "2026-10-12", candle_start_at: "2026-10-12T09:15:00+05:30", open: "50.2", volume: "954", tick_count: 2 });
  assert.equal(candle.open, 50.2);
  assert.equal(candle.volume, 954);
  assert.equal(candle.candle_start_at, "2026-10-12T09:15:00+05:30");

  const event = mapEvent({ id: 1, watchlist_id: 2, event_type: "ADDED", new_state: "ACTIVE", event_timestamp: "2026-10-12T09:38:04+05:30", detail: '{"episode_count":2}' });
  assert.deepEqual(event.detail, { episode_count: 2 });

  const history = mapHistory({ trading_session_id: 1, trading_date: "2026-10-12", session_status: "FINALIZED", stale_data_seconds: "149", qualified_count: "3" });
  assert.equal(history.stale_data_seconds, 149);
  assert.equal(history.qualified_count, 3);

  const instrument = mapInstrument({ id: 1, exchange: "NSE", symbol: "TITAN", mapping_status: "MAPPED", previous_close: "3400.05", is_nifty500: true });
  assert.equal(instrument.previous_close, 3400.05);
  assert.equal(instrument.is_nifty500, true);
});

test("the watchlist row carries both RSI horizons, nulls stay null", () => {
  const withRsi = mapWatchlist({
    current_state: "ACTIVE",
    daily_rsi: "63.2500",
    weekly_rsi: null,
    rsi_bias: "OVERBOUGHT",
    rsi_5m: "38.4000",
    rsi_state: "ENTRY",
  });
  assert.equal(withRsi.daily_rsi, 63.25);
  assert.equal(withRsi.weekly_rsi, null);
  assert.equal(withRsi.rsi_bias, "OVERBOUGHT");
  // The intraday reading arrives as NUMERIC like every other stored number.
  assert.equal(withRsi.rsi_5m, 38.4);
  assert.equal(withRsi.rsi_state, "ENTRY");

  // No stock_details row (or a half-written one) must read as "not classified".
  const without = mapWatchlist({ current_state: "ACTIVE", rsi_bias: null });
  assert.equal(without.daily_rsi, null);
  assert.equal(without.rsi_bias, null);
  // A source that does not select the intraday columns at all — the replay sample —
  // must leave them absent, not invent a zero the board would colour.
  assert.equal(without.rsi_5m, null);
  assert.equal(without.rsi_state, null);
});

test("a classification is only ever the three stored words, on either horizon", () => {
  const ok = mapStockOverview({ instrument_id: 1, rsi_bias: "NEUTRAL" });
  assert.equal(ok.rsi_bias, "NEUTRAL");
  // A stray string must degrade to null, not become a coloured claim on screen.
  const stray = mapStockOverview({ instrument_id: 1, rsi_bias: "entry" });
  assert.equal(stray.rsi_bias, null);
  const empty = mapStockOverview({ instrument_id: 1, rsi_bias: "" });
  assert.equal(empty.rsi_bias, null);
  // Migration 0011 translates BULLISH/BEARISH in place, so a row that still spells
  // them comes from a database this build has not been told how to read. Refuse it.
  const retired = mapStockOverview({ instrument_id: 1, rsi_bias: "BULLISH" });
  assert.equal(retired.rsi_bias, null);
  // The same gate governs the intraday state on a watchlist or snapshot row.
  assert.equal(mapWatchlist({ current_state: "ACTIVE", rsi_state: "BEARISH" }).rsi_state, null);
  assert.equal(mapSnapshot({ id: 1, rsi_state: "OVERBOUGHT" }).rsi_state, "OVERBOUGHT");
});

test("a snapshot carries the intraday RSI the detector fired on", () => {
  const snapshot = mapSnapshot({
    id: 3,
    instrument_id: "41",
    rsi_5m: "61.7500",
    rsi_state: "OVERBOUGHT",
  });
  assert.equal(snapshot.rsi_5m, 61.75);
  assert.equal(snapshot.rsi_state, "OVERBOUGHT");
  // Before the first bucket closes there is no reading and no verdict; the board
  // shows a dash and the alert rule shows nothing, so null must survive the map.
  const early = mapSnapshot({ id: 4, instrument_id: "41" });
  assert.equal(early.rsi_5m, null);
  assert.equal(early.rsi_state, null);
});

test("the stock overview maps NUMERIC strings and keeps missing values missing", () => {
  const row = mapStockOverview({
    instrument_id: "41",
    symbol: "RELIANCE",
    trading_symbol: "RELIANCE-EQ",
    company_name: "Reliance Industries Limited",
    exchange: "NSE",
    last_bar_date: "2026-10-09",
    close: "1159.1000",
    previous_close: "1170.5000",
    pe_ratio: "21.0000",
    market_cap_cr: "1569999.0000",
    listing_date: "1995-11-29",
    profile_source: "yahoo+equity_l",
    high_52w: "1611.8000",
    low_52w: "1114.8500",
    ath: "1611.8000",
    atl: "9.5000",
    data_start: "2002-01-01",
    daily_rsi: "63.2500",
    weekly_rsi: "61.8400",
    rsi_bias: "OVERBOUGHT",
    rsi_as_of: "2026-10-09",
  });
  assert.equal(row.instrument_id, 41);
  assert.equal(row.close, 1159.1);
  assert.equal(row.previous_close, 1170.5);
  assert.equal(row.pe_ratio, 21);
  assert.equal(row.market_cap_cr, 1569999);
  assert.equal(row.listing_date, "1995-11-29");
  assert.equal(row.daily_rsi, 63.25);
  assert.equal(row.weekly_rsi, 61.84);
  assert.equal(row.rsi_bias, "OVERBOUGHT");

  const bare = mapStockOverview({ instrument_id: 7, symbol: "NEWCO" });
  assert.equal(bare.close, null);
  assert.equal(bare.pe_ratio, null);
  assert.equal(bare.daily_rsi, null);
  assert.equal(bare.rsi_bias, null);
});

test("RSI series points normalise like every other row", () => {
  const daily = mapDailyRsiPoint({ trading_date: "2026-10-09", close: "1159.10", rsi14: "63.2500" });
  assert.deepEqual(daily, { trading_date: "2026-10-09", close: 1159.1, rsi14: 63.25 });
  const weekly = mapWeeklyRsiPoint({ week_start: "2026-10-05", close: "1160.00", rsi14: "61.8400" });
  assert.deepEqual(weekly, { week_start: "2026-10-05", close: 1160, rsi14: 61.84 });
});
