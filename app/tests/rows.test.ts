import assert from "node:assert/strict";
import { test } from "node:test";
import { mapCandle, mapConfig, mapCoverage, mapDelivery, mapEvent, mapHistory, mapInstrument, mapSignal, mapSnapshot, mapWatchlist, mapWorker, dateParam } from "../lib/rows";
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
    last_verified_at: new Date("2026-10-12T03:00:00Z"),
  });
  assert.equal(coverage.nifty500_count, 500);
  assert.equal(coverage.mapped_count, 480);
  assert.equal(coverage.ambiguous_count, 0);
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
