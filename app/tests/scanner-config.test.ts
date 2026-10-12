import assert from "node:assert/strict";
import { test } from "node:test";
import { parseConfigEdits } from "../lib/scanner-config";

/**
 * The editor's server-side validation: the same rules the worker's
 * `ScannerParams.validate` enforces, so a save that would make the worker
 * decline its own config at the next session start is rejected here with a
 * 400 instead of writing a broken row.
 */

const good = {
  fast_ema_period: "9",
  slow_ema_period: "20",
  volume_lookback: "20",
  relative_volume_threshold: "1.5",
  alert_cooldown_minutes: "15",
  removal_confirm_candles: "3",
  initial_scan_candles: "5",
};

test("a valid form parses to the typed values", () => {
  const parsed = parseConfigEdits(good);
  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(parsed.values.fast_ema_period, 9);
    assert.equal(parsed.values.slow_ema_period, 20);
    assert.equal(parsed.values.volume_lookback, 20);
    assert.equal(parsed.values.relative_volume_threshold, 1.5);
    assert.equal(parsed.values.alert_cooldown_minutes, 15);
    assert.equal(parsed.values.removal_confirm_candles, 3);
    assert.equal(parsed.values.initial_scan_candles, 5);
  }
});

test("slow EMA must exceed fast EMA — the worker would refuse the row", () => {
  const parsed = parseConfigEdits({ ...good, slow_ema_period: "9" });
  assert.equal(parsed.ok, false);
  if (!parsed.ok) assert.match(parsed.error, /greater than/);
});

test("fast EMA below 2 is rejected", () => {
  const parsed = parseConfigEdits({ ...good, fast_ema_period: "1" });
  assert.equal(parsed.ok, false);
});

test("relative volume threshold must be positive", () => {
  const parsed = parseConfigEdits({ ...good, relative_volume_threshold: "0" });
  assert.equal(parsed.ok, false);
});

test("non-numeric input is rejected before it reaches the database", () => {
  const parsed = parseConfigEdits({ ...good, volume_lookback: "abc" });
  assert.equal(parsed.ok, false);
  if (!parsed.ok) assert.match(parsed.error, /number/);
});

test("a missing field is treated as non-numeric, not as zero", () => {
  const missing: Record<string, string> = { ...good };
  delete missing.initial_scan_candles;
  const parsed = parseConfigEdits(missing);
  assert.equal(parsed.ok, false);
});

test("negative cooldown and zero removal confirmation are rejected", () => {
  assert.equal(parseConfigEdits({ ...good, alert_cooldown_minutes: "-1" }).ok, false);
  assert.equal(parseConfigEdits({ ...good, removal_confirm_candles: "0" }).ok, false);
});

test("floats where integers belong are truncated, not errored", () => {
  const parsed = parseConfigEdits({ ...good, fast_ema_period: "9.7" });
  assert.equal(parsed.ok, true);
  if (parsed.ok) assert.equal(parsed.values.fast_ema_period, 9);
});
