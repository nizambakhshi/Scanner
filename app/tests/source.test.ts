import "./env-setup";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { replaySource } from "../lib/source-replay";
import { isValidIst } from "../lib/format";

/**
 * The replay sample is served by the same interface the database is served by, so
 * this test is really about the contract: the shapes, the ordering, and the two
 * things that must never appear anywhere in a response — a broker instrument id and
 * a delivery destination.
 *
 * It also re-reads the JSON on disk directly, because the exporter's own assertion
 * compares against the migration; if the file drifts from what the pages expect,
 * that has to fail here rather than render an empty cell.
 */

const IST_KEYS = [
  "event_timestamp",
  "candle_start_at",
  "initial_entry_at",
  "latest_state_change_at",
  "latest_candle_at",
  "last_market_data_at",
  "last_completed_candle_at",
  "session_open_at",
  "session_close_at",
  "initial_scan_completed_at",
  "finalized_at",
  "updated_at",
  "started_at",
  "last_verified_at",
  "delivered_at",
  "next_retry_at",
  "last_attempt_at",
  "source_timestamp",
  "completed_at",
];

const FORBIDDEN = [
  "instrument_token",
  "destination_reference",
  "password_hash",
  "password",
  "api_key",
  "jwt_token",
  "refresh_token",
  "totp",
  "pin",
  "telegram_bot_token",
];

test("the sample announces itself as simulated", async () => {
  const meta = await replaySource.meta();
  assert.equal(meta.source, "replay");
  assert.equal(meta.simulated, true);
  assert.ok(meta.banner);
  assert.match(meta.banner ?? "", /SIMULATED/i);
  assert.match(meta.tradingDate ?? "", /^\d{4}-\d{2}-\d{2}$/);
});

test("no broker or provider identifier appears anywhere in the sample", () => {
  // A deep scan of the shipped file, not of the mapped rows: a column the mapper
  // does not know about is exactly the thing this is guarding.
  const raw = JSON.parse(readFileSync(join(process.cwd(), "data", "replay-fixture.json"), "utf8")) as unknown;
  const offenders: string[] = [];
  const walk = (value: unknown, path: string) => {
    if (Array.isArray(value)) {
      value.forEach((item, index) => walk(item, `${path}[${index}]`));
      return;
    }
    if (value && typeof value === "object") {
      for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
        if (FORBIDDEN.includes(key.toLowerCase())) offenders.push(`${path}.${key}`);
        walk(child, `${path}.${key}`);
      }
    }
  };
  walk(raw, "fixture");
  assert.deepEqual(offenders, []);
});

test("every timestamp the pages format is already Asia/Kolkata", async () => {
  const [watchlist, signals, history, workers, coverage, scan] = await Promise.all([
    replaySource.currentWatchlist(),
    replaySource.signals({ limit: 500 }),
    replaySource.history(30),
    replaySource.workerStatus(),
    replaySource.coverage(),
    replaySource.initialScan(),
  ]);
  const rows: Record<string, unknown>[] = [...watchlist, ...signals, ...history, ...workers, coverage ?? {}, scan ?? {}];
  for (const row of rows) {
    for (const key of IST_KEYS) {
      if (!(key in row)) continue;
      const value = row[key];
      if (value === null) continue;
      assert.ok(isValidIst(String(value)), `${key} = ${String(value)} is not an IST ISO timestamp`);
    }
  }
});

test("the watchlist is ordered the way the view orders it", async () => {
  const rows = await replaySource.currentWatchlist();
  assert.ok(rows.length > 0);
  for (let index = 1; index < rows.length; index += 1) {
    const before = rows[index - 1];
    const after = rows[index];
    assert.equal(
      Number(before.is_active) > Number(after.is_active) ||
        Number(before.is_active) === Number(after.is_active),
      true,
      "active memberships must come first",
    );
    if (Number(before.is_active) === Number(after.is_active)) {
      assert.ok((before.latest_score ?? -1) >= (after.latest_score ?? -1), "score must fall within a state group");
    }
  }
});

test("the alert feed is newest-first and every filter actually filters", async () => {
  const all = await replaySource.signals({ limit: 500 });
  for (let index = 1; index < all.length; index += 1) {
    assert.ok(all[index - 1].event_timestamp >= all[index].event_timestamp);
  }
  const symbol = all.find((row) => row.symbol)?.symbol ?? "";
  const filtered = await replaySource.signals({ symbol });
  assert.ok(filtered.length > 0);
  assert.ok(filtered.every((row) => (row.symbol ?? "").toUpperCase() === symbol.toUpperCase()));

  const typed = await replaySource.signals({ signalType: "BULLISH_ENTRY" });
  assert.ok(typed.every((row) => row.signal_type === "BULLISH_ENTRY"));

  const dated = await replaySource.signals({ tradingDate: "1999-01-01" });
  assert.deepEqual(dated, [], "a day the worker never ran must be empty, not everything");

  const capped = await replaySource.signals({ limit: 3 });
  assert.equal(capped.length, 3);
});

test("an instrument read returns candles with their indicators attached", async () => {
  const detail = await replaySource.instrument("SIMFADE");
  assert.ok(detail);
  assert.equal(detail.instrument?.symbol, "SIMFADE");
  assert.ok(detail.candles.length > 0);
  for (const candle of detail.candles) {
    assert.ok(candle.high >= candle.low, `${candle.candle_start_at}: high below low`);
    assert.ok(candle.high >= Math.max(candle.open, candle.close));
    assert.ok(candle.low <= Math.min(candle.open, candle.close));
    assert.ok(candle.volume >= 0);
  }
  // The chart aligns snapshots to candles by their start minute; that only works if
  // both are ordered oldest-first.
  const starts = detail.candles.map((candle) => candle.candle_start_at);
  assert.deepEqual(starts, [...starts].sort());
  assert.ok(detail.snapshots.every((snapshot, index) => index === 0 || snapshot.candle_start_at >= detail.snapshots[index - 1].candle_start_at));
});

test("an unknown symbol is not found rather than empty", async () => {
  assert.equal(await replaySource.instrument("NOT_A_SYMBOL"), null);
});

test("the replay sample serves the stocks directory honestly empty", async () => {
  // The fixture predates candles_1d/stock_details (the nightly syncs); the
  // /stocks page and the RSI panel show their empty state under the replay
  // banner rather than fabricated numbers.
  assert.deepEqual(await replaySource.stocksOverview(), []);
  const detail = await replaySource.instrument("SIMFADE");
  assert.ok(detail);
  assert.equal(detail.overview, null);
  assert.deepEqual(detail.daily_rsi, []);
  assert.deepEqual(detail.weekly_rsi, []);
});

test("deliveries only come back for the signals asked about", async () => {
  const signals = await replaySource.signals({ limit: 5 });
  const ids = signals.map((row) => row.id);
  const deliveries = await replaySource.deliveries(ids);
  assert.ok(deliveries.length > 0);
  assert.ok(deliveries.every((row) => ids.includes(row.signal_id)));
  assert.deepEqual(await replaySource.deliveries([]), []);
});

test("coverage and config describe the universe the replay actually loaded", async () => {
  const coverage = await replaySource.coverage();
  assert.ok(coverage);
  assert.equal(coverage.nifty500_count, coverage.mapped_count + coverage.unmapped_count + coverage.ambiguous_count + coverage.invalid_count);
  const config = await replaySource.config();
  assert.ok(config);
  assert.equal(config.fast_ema_period, 9);
  assert.equal(config.slow_ema_period, 20);
  assert.ok(Object.values(config.rank_weights).every((weight) => weight >= 0 && weight <= 1));
});

test("the session summary counts are internally consistent", async () => {
  const [day] = await replaySource.history(1);
  assert.ok(day);
  assert.ok(day.monitored_count <= day.subscribed_count, "you cannot monitor what was never subscribed");
  assert.ok(day.mapped_count <= day.universe_count);
  assert.equal(day.telegram_delivered + day.telegram_failed, 0, "simulation mode must not claim deliveries");
});
