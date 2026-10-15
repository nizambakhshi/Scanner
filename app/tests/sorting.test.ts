import { test } from "node:test";
import assert from "node:assert/strict";
import {
  compareMembers,
  compareStocks,
  memberSortDir,
  parseMemberSort,
  parseSortDir,
  parseStockSort,
  stockDayChange,
  stockSortDir,
  toggledDir,
} from "../lib/sorting";
import type { StockOverviewRow, WatchlistRow } from "../lib/types";

/**
 * The sorting contract: nulls last in both directions, the source's order kept
 * on ties, and the parse/toggle rules the page wires to the headers.
 */

function member(overrides: Partial<WatchlistRow>): WatchlistRow {
  return {
    watchlist_id: 1,
    trading_session_id: 1,
    trading_date: "2026-10-15",
    instrument_id: 1,
    symbol: "A",
    trading_symbol: "A-EQ",
    company_name: null,
    exchange: "NSE",
    current_state: "ACTIVE",
    initial_entry_at: null,
    latest_state_change_at: null,
    initial_entry_price: null,
    latest_price: null,
    latest_score: null,
    latest_rank: null,
    removal_reason: null,
    consecutive_weak_candles: 0,
    episode_count: 1,
    is_active: true,
    is_simulated: false,
    vwap: null,
    ema9: null,
    ema20: null,
    relative_volume: null,
    intraday_change_pct: null,
    distance_from_vwap_pct: null,
    ema_spread_pct: null,
    session_high: null,
    session_low: null,
    distance_from_high_pct: null,
    bullish_qualified: true,
    reasons: [],
    failed_reasons: [],
    data_quality: null,
    warming_up: false,
    latest_candle_at: null,
    indicator_snapshot_id: null,
    daily_rsi: null,
    weekly_rsi: null,
    rsi_bias: null,
    rsi_5m: null,
    rsi_state: null,
    ...overrides,
  };
}

function stock(overrides: Partial<StockOverviewRow>): StockOverviewRow {
  return {
    instrument_id: 1,
    symbol: "A",
    trading_symbol: "A-EQ",
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
    rsi_bias: null,
    rsi_as_of: null,
    ...overrides,
  };
}

test("numbers sort both ways and nulls stay last in both", () => {
  const rows = [member({ symbol: "B", latest_score: 5 }), member({ symbol: "A", latest_score: null }), member({ symbol: "C", latest_score: 9 })];
  const desc = [...rows].sort(compareMembers("desc", "score")).map((row) => row.symbol);
  const asc = [...rows].sort(compareMembers("asc", "score")).map((row) => row.symbol);
  assert.deepEqual(desc, ["C", "B", "A"]);
  // The unread row must not lead just because the arrow flipped.
  assert.deepEqual(asc, ["B", "C", "A"]);
});

test("ties keep the source's order — the default ranking is what remains", () => {
  const rows = [
    member({ symbol: "FIRST", latest_score: 7 }),
    member({ symbol: "SECOND", latest_score: 7 }),
    member({ symbol: "THIRD", latest_score: 7 }),
  ];
  const sorted = [...rows].sort(compareMembers("desc", "score")).map((row) => row.symbol);
  assert.deepEqual(sorted, ["FIRST", "SECOND", "THIRD"]);
});

test("the universe's day change is the two-number formula, and a zero base is unread", () => {
  assert.equal(stockDayChange(stock({ close: 110, previous_close: 100 })), 10);
  assert.equal(stockDayChange(stock({ close: 95, previous_close: 100 })), -5);
  assert.equal(stockDayChange(stock({ close: 95, previous_close: 0 })), null);
  assert.equal(stockDayChange(stock({ close: 95, previous_close: null })), null);
  const rows = [
    stock({ symbol: "UP", close: 110, previous_close: 100 }),
    stock({ symbol: "NOREAD", close: 95, previous_close: null }),
    stock({ symbol: "DOWN", close: 95, previous_close: 100 }),
  ];
  const desc = [...rows].sort(compareStocks("desc", "change")).map((row) => row.symbol);
  const asc = [...rows].sort(compareStocks("asc", "change")).map((row) => row.symbol);
  assert.deepEqual(desc, ["UP", "DOWN", "NOREAD"]);
  assert.deepEqual(asc, ["DOWN", "UP", "NOREAD"]);
});

test("the sector sort uses the chip's label, and unmapped rows sort last", () => {
  const rows = [
    member({ symbol: "SBIN" }),
    member({ symbol: "SIMBREAK" }), // not a constituent of any tracked index
    member({ symbol: "TCS" }),
  ];
  const asc = [...rows].sort(compareMembers("asc", "sector")).map((row) => row.symbol);
  assert.deepEqual(asc, ["TCS", "SBIN", "SIMBREAK"]);
  const desc = [...rows].sort(compareMembers("desc", "sector")).map((row) => row.symbol);
  assert.deepEqual(desc, ["SBIN", "TCS", "SIMBREAK"]);
});

test("state ascends from healthy to degraded; the bias sort leads with entries", () => {
  const rows = [
    member({ symbol: "REMOVED", current_state: "REMOVED" }),
    member({ symbol: "WEAK", current_state: "WEAKENING" }),
    member({ symbol: "LIVE", current_state: "ACTIVE" }),
    member({ symbol: "BACK", current_state: "REQUALIFIED" }),
  ];
  const asc = [...rows].sort(compareMembers("asc", "state")).map((row) => row.symbol);
  assert.deepEqual(asc, ["LIVE", "BACK", "WEAK", "REMOVED"]);

  const sides = [
    stock({ symbol: "OVER", rsi_bias: "OVERBOUGHT" }),
    stock({ symbol: "NEUT", rsi_bias: "NEUTRAL" }),
    stock({ symbol: "ENT", rsi_bias: "ENTRY" }),
    stock({ symbol: "NONE", rsi_bias: null }),
  ];
  const bySide = [...sides].sort(compareStocks("asc", "side")).map((row) => row.symbol);
  assert.deepEqual(bySide, ["ENT", "NEUT", "OVER", "NONE"]);
});

test("parsing: unknown keys are null, case is tolerated, a repeated param takes the first", () => {
  assert.equal(parseMemberSort("score"), "score");
  assert.equal(parseMemberSort("SCORE"), "score");
  assert.equal(parseMemberSort("nonsense"), null);
  assert.equal(parseMemberSort(undefined), null);
  assert.equal(parseMemberSort(["rsi", "rank"]), "rsi");
  assert.equal(parseStockSort("mcap"), "mcap");
  assert.equal(parseStockSort("rsi"), null); // member-only key is not a stock key
  assert.equal(parseSortDir("asc", "desc"), "asc");
  assert.equal(parseSortDir("DOWN", "desc"), "desc");
  assert.equal(parseSortDir(undefined, "desc"), "desc");
});

test("the first click on a column uses its own default; clicking it again flips", () => {
  assert.equal(memberSortDir("rank"), "asc"); // 1 is the best rank
  assert.equal(memberSortDir("rsi"), "asc"); // the dip hunt reads small first
  assert.equal(memberSortDir("score"), "desc");
  assert.equal(stockSortDir("symbol"), "asc");
  assert.equal(stockSortDir("listed"), "desc"); // newest listings first
  assert.equal(toggledDir("desc", true, "desc"), "asc");
  assert.equal(toggledDir("asc", true, "desc"), "desc");
  assert.equal(toggledDir("desc", false, "asc"), "asc"); // a new column ignores the old direction
});
