/**
 * The filter vocabulary for the two directory boards.
 *
 * Both /stocks and /strategy show a row of tally chips above their table, and
 * every chip is also the filter for its own count — "Entry 12" both reports and
 * selects. The selection lives in the URL (`?bias=…`, `?status=…`) rather than
 * client state: the pages are server-rendered, the cut stays shareable, and the
 * back button works. An unknown value parses to ALL — a stale bookmark must show
 * the whole board, never an unexplained empty table.
 */

import type { OrbView, SignalType, StockOverviewRow, WatchlistRow } from "./types";

/**
 * Every signal type a row can carry, in the order the feed lists them.
 *
 * One list rather than two, because the page's chips and the API's allowlist answer
 * the same question — "is this a type a signal row can have?" — and when they drift,
 * a filter a person can select becomes a query the endpoint refuses. The retired
 * `RSI_BULLISH` / `RSI_BEARISH` pair stays: migration 0011 retired the vocabulary but
 * kept the rows written under it readable, and a feed that cannot select its own
 * history is a feed with unreachable rows.
 */
export const SIGNAL_TYPES: readonly SignalType[] = [
  "BULLISH_ENTRY",
  "WATCHLIST_ADDED",
  "WATCHLIST_WEAKENING",
  "WATCHLIST_REMOVED",
  "WATCHLIST_REQUALIFIED",
  "DATA_FEED_INTERRUPTED",
  "DATA_FEED_RECOVERED",
  "DAILY_SCAN_COMPLETE",
  "RSI_ENTRY",
  "RSI_OVERBOUGHT",
  "RSI_BULLISH",
  "RSI_BEARISH",
  "BREAKOUT_UP",
  "BREAKDOWN",
];

const SIGNAL_TYPE_SET = new Set<string>(SIGNAL_TYPES);

/** A query-string type, upper-cased for the person who typed it lowercase, or `null`
 *  when no row could ever carry it. */
export function parseSignalType(raw: string | string[] | undefined | null): SignalType | null {
  const value = (Array.isArray(raw) ? (raw[0] ?? "") : (raw ?? "")).toUpperCase();
  return SIGNAL_TYPE_SET.has(value) ? (value as SignalType) : null;
}

export type StockBiasFilter = "ALL" | "CLASSIFIED" | "ENTRY" | "OVERBOUGHT" | "NEUTRAL";

const STOCK_BIAS_FILTERS: readonly string[] = ["CLASSIFIED", "ENTRY", "OVERBOUGHT", "NEUTRAL"];

export function parseStockBiasFilter(raw: string | string[] | undefined): StockBiasFilter {
  const value = (Array.isArray(raw) ? raw[0] : raw)?.toUpperCase() ?? "";
  return (STOCK_BIAS_FILTERS.includes(value) ? value : "ALL") as StockBiasFilter;
}

/** `CLASSIFIED` is the "the daily/weekly sync has covered this symbol" cut — a
 *  null bias is "not classified yet", and it must not be lumped into NEUTRAL,
 *  because neutral is a measurement and absence is not.
 *
 *  A bookmark from before 2026-10-15 says `?bias=BULLISH`; that is not one of the
 *  four values this build filters on, so it falls through to the whole universe
 *  rather than quietly selecting nothing. */
export function filterStocksByBias(rows: StockOverviewRow[], filter: StockBiasFilter): StockOverviewRow[] {
  switch (filter) {
    case "CLASSIFIED":
      return rows.filter((row) => row.rsi_bias !== null);
    case "ENTRY":
      return rows.filter((row) => row.rsi_bias === "ENTRY");
    case "OVERBOUGHT":
      return rows.filter((row) => row.rsi_bias === "OVERBOUGHT");
    case "NEUTRAL":
      return rows.filter((row) => row.rsi_bias === "NEUTRAL");
    default:
      return rows;
  }
}

export type WatchlistStateFilter = "ALL" | "TRACKED" | "WEAKENING" | "REMOVED";

const WATCHLIST_STATE_FILTERS: readonly string[] = ["TRACKED", "WEAKENING", "REMOVED"];

export function parseWatchlistStateFilter(raw: string | string[] | undefined): WatchlistStateFilter {
  const value = (Array.isArray(raw) ? raw[0] : raw)?.toUpperCase() ?? "";
  return (WATCHLIST_STATE_FILTERS.includes(value) ? value : "ALL") as WatchlistStateFilter;
}

/** `TRACKED` is "on the list right now" (ACTIVE or REQUALIFIED); `WEAKENING`
 *  groups the two degraded-but-present states, including DATA_STALE — a frozen
 *  feed is a membership that needs attention, not a removal. A WARMING_UP row
 *  is in none of the three cuts: it is neither tracked (not yet judged), nor
 *  weakening, nor removed — it appears under ALL, and that is the honest place
 *  for a state that has not produced a verdict yet. */
export function filterWatchlistByState(rows: WatchlistRow[], filter: WatchlistStateFilter): WatchlistRow[] {
  switch (filter) {
    case "TRACKED":
      return rows.filter((row) => row.current_state === "ACTIVE" || row.current_state === "REQUALIFIED");
    case "WEAKENING":
      return rows.filter((row) => row.current_state === "WEAKENING" || row.current_state === "DATA_STALE");
    case "REMOVED":
      return rows.filter((row) => row.current_state === "REMOVED");
    default:
      return rows;
  }
}

export type OrbStatusFilter = "ALL" | "BROKE_UP" | "BROKE_DOWN" | "CONFIRMED";

const ORB_STATUS_FILTERS: readonly string[] = ["BROKE_UP", "BROKE_DOWN", "CONFIRMED"];

export function parseOrbStatusFilter(raw: string | string[] | undefined): OrbStatusFilter {
  const value = (Array.isArray(raw) ? raw[0] : raw)?.toUpperCase() ?? "";
  return (ORB_STATUS_FILTERS.includes(value) ? value : "ALL") as OrbStatusFilter;
}

/** `CONFIRMED` is the strategy's full setup — a breakout whose three filter
 *  readings are all true. An unknown EMA is not confirmation, so those rows
 *  stay out of this cut (the board itself shows them with a dash). */
export function filterOrbViews(views: OrbView[], filter: OrbStatusFilter): OrbView[] {
  switch (filter) {
    case "BROKE_UP":
      return views.filter((view) => view.status === "BROKE_UP");
    case "BROKE_DOWN":
      return views.filter((view) => view.status === "BROKE_DOWN");
    case "CONFIRMED":
      return views.filter((view) => view.confirmed);
    default:
      return views;
  }
}
