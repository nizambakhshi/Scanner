import assert from "node:assert/strict";
import { test } from "node:test";
import {
  SIGNAL_TYPES,
  filterOrbViews,
  filterStocksByBias,
  parseOrbStatusFilter,
  parseSignalType,
  parseStockBiasFilter,
} from "@/lib/filters";
import type { OrbRow, OrbView, RsiBias, StockOverviewRow } from "@/lib/types";

/** Fixtures carry only what the filters read; the rest is honestly blank. */
function stock(symbol: string, bias: RsiBias): StockOverviewRow {
  return {
    instrument_id: 1,
    symbol,
    trading_symbol: `${symbol}-EQ`,
    company_name: null,
    exchange: "NSE",
    last_bar_date: null,
    close: null,
    previous_close: null,
    pe_ratio: null,
    market_cap_cr: null,
    listing_date: null,
    profile_source: null,
    high_52w: null,
    low_52w: null,
    ath: null,
    atl: null,
    data_start: null,
    daily_rsi: null,
    weekly_rsi: null,
    rsi_bias: bias,
    rsi_as_of: null,
  };
}

function orbRow(symbol: string): OrbRow {
  return {
    instrument_id: 1,
    symbol,
    trading_symbol: `${symbol}-EQ`,
    company_name: null,
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
    latest_at: "2026-10-12T10:00:00+05:30",
    latest_close: 100.0,
    session_vwap: 99.9,
    ema9: null,
    ema20: null,
    snap_at: null,
  };
}

function view(symbol: string, overrides: Partial<OrbView>): OrbView {
  return {
    row: orbRow(symbol),
    status: "INSIDE",
    direction: null,
    retreated: false,
    width: null,
    width_pct: null,
    stop: null,
    target1: null,
    target2: null,
    vwap_side: null,
    vwap_agrees: null,
    volume_agrees: null,
    ema_agrees: null,
    confirmed: false,
    ...overrides,
  };
}

const UNIVERSE = [
  stock("ENT1", "ENTRY"),
  stock("OVR1", "OVERBOUGHT"),
  stock("NEUT1", "NEUTRAL"),
  stock("NEUT2", "NEUTRAL"),
  stock("RAW1", null),
];

test("an unknown or absent bias filter shows the whole universe", () => {
  assert.equal(parseStockBiasFilter(undefined), "ALL");
  assert.equal(parseStockBiasFilter(""), "ALL");
  assert.equal(parseStockBiasFilter("wat"), "ALL");
  // A bookmark from before the 2026-10-15 vocabulary says `?bias=BULLISH`. It selects
  // nothing on purpose — showing the whole board is a visible miss, while quietly
  // mapping an old word onto a new one would put a coloured claim on screen that the
  // stored row never made.
  assert.equal(parseStockBiasFilter("bullish"), "ALL");
  assert.equal(parseStockBiasFilter("BEARISH"), "ALL");
  const all = filterStocksByBias(UNIVERSE, parseStockBiasFilter(undefined));
  assert.equal(all.length, UNIVERSE.length);
});

test("the bias filter parses case-insensitively and takes the first array value", () => {
  assert.equal(parseStockBiasFilter("entry"), "ENTRY");
  assert.equal(parseStockBiasFilter(["OVERBOUGHT", "ENTRY"]), "OVERBOUGHT");
  assert.equal(parseStockBiasFilter("neutral"), "NEUTRAL");
});

test("CLASSIFIED keeps only symbols the sync has covered — a null bias is not neutral", () => {
  const rows = filterStocksByBias(UNIVERSE, "CLASSIFIED");
  assert.deepEqual(
    rows.map((row) => row.symbol),
    ["ENT1", "OVR1", "NEUT1", "NEUT2"],
  );
});

test("each bias filter selects exactly its own rows", () => {
  const symbols = (filter: Parameters<typeof filterStocksByBias>[1]) =>
    filterStocksByBias(UNIVERSE, filter).map((row) => row.symbol);
  assert.deepEqual(symbols("ENTRY"), ["ENT1"]);
  assert.deepEqual(symbols("OVERBOUGHT"), ["OVR1"]);
  assert.deepEqual(symbols("NEUTRAL"), ["NEUT1", "NEUT2"]);
  assert.deepEqual(symbols("ALL"), ["ENT1", "OVR1", "NEUT1", "NEUT2", "RAW1"]);
});

test("the signal-type list accepts the current vocabulary and the retired one", () => {
  assert.equal(parseSignalType("rsi_entry"), "RSI_ENTRY");
  assert.equal(parseSignalType(["BREAKDOWN", "RSI_OVERBOUGHT"]), "BREAKDOWN");
  // Rows written before 2026-10-15 must stay selectable, or the feed's own history
  // becomes unreachable the day the vocabulary changes.
  assert.equal(parseSignalType("RSI_BULLISH"), "RSI_BULLISH");
  assert.equal(parseSignalType("BULLISH"), null, "that word is a bias, never a signal type");
  assert.equal(parseSignalType(""), null);
  assert.equal(parseSignalType(undefined), null);
  // The endpoint's allowlist and the page's dropdown are one list: every type the
  // detectors write has to be in it.
  for (const type of ["BREAKOUT_UP", "BREAKDOWN", "RSI_ENTRY", "RSI_OVERBOUGHT", "BULLISH_ENTRY"]) {
    assert.ok(SIGNAL_TYPES.includes(type as (typeof SIGNAL_TYPES)[number]), `${type} must be filterable`);
  }
});

const VIEWS: OrbView[] = [
  view("UP1", { status: "BROKE_UP", direction: "UP", confirmed: true }),
  view("UP2", { status: "BROKE_UP", direction: "UP", confirmed: false }),
  view("DOWN1", { status: "BROKE_DOWN", direction: "DOWN", confirmed: false }),
  view("FLAT1", { status: "INSIDE" }),
];

test("the status filter parses the same way — an unknown status is the full board", () => {
  assert.equal(parseOrbStatusFilter("confirmed"), "CONFIRMED");
  assert.equal(parseOrbStatusFilter("wat"), "ALL");
  assert.equal(filterOrbViews(VIEWS, "ALL").length, VIEWS.length);
});

test("BROKE UP and BROKE DOWN select by the first breakout's side", () => {
  assert.deepEqual(
    filterOrbViews(VIEWS, "BROKE_UP").map((v) => v.row.symbol),
    ["UP1", "UP2"],
  );
  assert.deepEqual(
    filterOrbViews(VIEWS, "BROKE_DOWN").map((v) => v.row.symbol),
    ["DOWN1"],
  );
});

test("CONFIRMED needs all three filters — a breakout with an unknown EMA stays out", () => {
  assert.deepEqual(
    filterOrbViews(VIEWS, "CONFIRMED").map((v) => v.row.symbol),
    ["UP1"],
  );
});
