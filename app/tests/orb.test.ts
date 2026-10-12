import assert from "node:assert/strict";
import { test } from "node:test";
import { ORB_VOLUME_MULTIPLE, compareOrb, deriveOrb } from "@/lib/orb";
import type { OrbRow } from "@/lib/types";

/** A fully-formed session row; each test overrides only what it exercises. */
function row(overrides: Partial<OrbRow>): OrbRow {
  return {
    instrument_id: 1,
    symbol: "TESTCO",
    trading_symbol: "TESTCO-EQ",
    company_name: "Test Co",
    session_date: "2026-10-12",
    or_high: 100.5,
    or_low: 99.0,
    or_bars: 5,
    bu_at: null,
    bu_close: null,
    bu_volume: null,
    bu_ratio: null,
    bd_at: null,
    bd_close: null,
    bd_volume: null,
    bd_ratio: null,
    latest_at: "2026-10-12T15:29:00+05:30",
    latest_close: 101.0,
    session_vwap: 100.0,
    ema9: 101.2,
    ema20: 100.8,
    snap_at: "2026-10-12T15:29:00+05:30",
    ...overrides,
  };
}

test("no range means no strategy — NO_DATA, not a zero range", () => {
  const view = deriveOrb(row({ or_high: null, or_low: null, or_bars: null, latest_at: null, latest_close: null }));
  assert.equal(view.status, "NO_DATA");
  assert.equal(view.direction, null);
  assert.equal(view.width, null);
  assert.equal(view.confirmed, false);
});

test("a session that never got past the range is OPENING, not INSIDE", () => {
  // The newest bucket is still the 9:15–9:20 candle itself.
  const view = deriveOrb(row({ latest_at: "2026-10-12T09:19:00+05:30", latest_close: 100.1 }));
  assert.equal(view.status, "OPENING");
  // The first post-range close flips it to INSIDE.
  const later = deriveOrb(row({ latest_at: "2026-10-12T09:24:00+05:30" }));
  assert.equal(later.status, "INSIDE");
});

test("an up breakout takes the short stop and 1×/2× range targets from the broken edge", () => {
  const view = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bu_close: 100.8, bu_volume: 9000, bu_ratio: 1.8 }),
  );
  assert.equal(view.status, "BROKE_UP");
  assert.equal(view.direction, "UP");
  const width = 100.5 - 99.0;
  assert.equal(view.width, width);
  assert.equal(view.stop, 99.0);
  assert.equal(view.target1, 100.5 + width);
  assert.equal(view.target2, 100.5 + 2 * width);
  assert.equal(view.width_pct, (width / 99.0) * 100);
});

test("a down breakout mirrors the levels", () => {
  const view = deriveOrb(
    row({ bd_at: "2026-10-12T10:10:00+05:30", bd_close: 98.7, bd_volume: 7000, bd_ratio: 2.1, latest_close: 98.5 }),
  );
  assert.equal(view.status, "BROKE_DOWN");
  assert.equal(view.stop, 100.5);
  assert.equal(view.target1, 99.0 - 1.5);
  assert.equal(view.target2, 99.0 - 3.0);
});

test("when both sides were broken, the earlier close is the signal", () => {
  const up = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bd_at: "2026-10-12T10:10:00+05:30" }),
  );
  assert.equal(up.direction, "UP");
  const down = deriveOrb(
    row({ bu_at: "2026-10-12T10:10:00+05:30", bd_at: "2026-10-12T09:25:00+05:30" }),
  );
  assert.equal(down.direction, "DOWN");
});

test("a breakout whose price fell back inside is flagged as retreated", () => {
  const back = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bu_close: 100.8, bu_ratio: 1.8, latest_close: 100.2 }),
  );
  assert.equal(back.retreated, true);
  const still = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bu_close: 100.8, bu_ratio: 1.8, latest_close: 101.4 }),
  );
  assert.equal(still.retreated, false);
});

test("VWAP agreement is judged only against a direction, and equality is no claim", () => {
  const up = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", bu_ratio: 1.8 }));
  assert.equal(up.vwap_side, "above");
  assert.equal(up.vwap_agrees, true);

  // Short with price still above VWAP: the filter fails.
  const shortAgainst = deriveOrb(
    row({ bd_at: "2026-10-12T09:25:00+05:30", bd_ratio: 1.8, latest_close: 100.2 }),
  );
  assert.equal(shortAgainst.vwap_agrees, false);

  // Price exactly at VWAP is not a side.
  const atVwap = deriveOrb(row({ latest_close: 100.0 }));
  assert.equal(atVwap.vwap_side, null);
  assert.equal(atVwap.vwap_agrees, null);

  // No breakout direction: the side is shown, agreement cannot be judged.
  const inside = deriveOrb(row({}));
  assert.equal(inside.vwap_side, "above");
  assert.equal(inside.vwap_agrees, null);
});

test("the volume filter is 1.5× and a missing ratio is unknown, not a pass", () => {
  assert.ok(ORB_VOLUME_MULTIPLE === 1.5);
  const pass = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", bu_ratio: 1.5 }));
  assert.equal(pass.volume_agrees, true);
  const fail = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", bu_ratio: 1.49 }));
  assert.equal(fail.volume_agrees, false);
  const unknown = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", bu_ratio: null }));
  assert.equal(unknown.volume_agrees, null);
});

test("EMA agreement needs a direction, both values, and a strict ordering", () => {
  const agree = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", ema9: 100.9, ema20: 100.4 }));
  assert.equal(agree.ema_agrees, true);
  const against = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", ema9: 100.1, ema20: 100.4 }));
  assert.equal(against.ema_agrees, false);
  const equal = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", ema9: 100.4, ema20: 100.4 }));
  assert.equal(equal.ema_agrees, null);
  const missing = deriveOrb(row({ bu_at: "2026-10-12T09:25:00+05:30", ema9: null, ema20: null }));
  assert.equal(missing.ema_agrees, null);
  const inside = deriveOrb(row({}));
  assert.equal(inside.ema_agrees, null);
});

test("confirmed needs a breakout and all three filters — an unknown EMA is not confirmation", () => {
  const all = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bu_close: 100.8, bu_ratio: 1.8, latest_close: 101.2, session_vwap: 100.0, ema9: 101.0, ema20: 100.5 }),
  );
  assert.equal(all.confirmed, true);
  const missingEma = deriveOrb(
    row({ bu_at: "2026-10-12T09:25:00+05:30", bu_ratio: 1.8, ema9: null, ema20: null }),
  );
  assert.equal(missingEma.confirmed, false);
  const noBreakout = deriveOrb(row({}));
  assert.equal(noBreakout.confirmed, false);
});

test("the board sorts breakouts first by time, then inside rows by widest range", () => {
  const up = deriveOrb(row({ instrument_id: 1, symbol: "AAA", bu_at: "2026-10-12T09:40:00+05:30", bu_ratio: 1.8 }));
  const down = deriveOrb(row({ instrument_id: 2, symbol: "BBB", bd_at: "2026-10-12T09:25:00+05:30", bd_ratio: 1.8 }));
  const wide = deriveOrb(row({ instrument_id: 3, symbol: "CCC", or_high: 110, or_low: 99 }));
  const narrow = deriveOrb(row({ instrument_id: 4, symbol: "DDD", or_high: 100.5, or_low: 100.0 }));
  const opening = deriveOrb(row({ instrument_id: 5, symbol: "EEE", latest_at: "2026-10-12T09:19:00+05:30" }));
  const empty = deriveOrb(row({ instrument_id: 6, symbol: "FFF", or_high: null, or_low: null, latest_at: null }));

  const sorted = [empty, up, narrow, opening, down, wide].sort(compareOrb).map((view) => view.row.symbol);
  assert.deepEqual(sorted, ["BBB", "AAA", "CCC", "DDD", "EEE", "FFF"]);
});
