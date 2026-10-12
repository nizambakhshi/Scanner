/**
 * The filter vocabulary for the two directory boards.
 *
 * Both /stocks and /strategy show a row of tally chips above their table, and
 * every chip is also the filter for its own count — "Bullish 3" both reports and
 * selects. The selection lives in the URL (`?bias=…`, `?status=…`) rather than
 * client state: the pages are server-rendered, the cut stays shareable, and the
 * back button works. An unknown value parses to ALL — a stale bookmark must show
 * the whole board, never an unexplained empty table.
 */

import type { OrbView, StockOverviewRow } from "./types";

export type StockBiasFilter = "ALL" | "CLASSIFIED" | "BULLISH" | "BEARISH" | "NEUTRAL";

const STOCK_BIAS_FILTERS: readonly string[] = ["CLASSIFIED", "BULLISH", "BEARISH", "NEUTRAL"];

export function parseStockBiasFilter(raw: string | string[] | undefined): StockBiasFilter {
  const value = (Array.isArray(raw) ? raw[0] : raw)?.toUpperCase() ?? "";
  return (STOCK_BIAS_FILTERS.includes(value) ? value : "ALL") as StockBiasFilter;
}

/** `CLASSIFIED` is the "the daily/weekly sync has covered this symbol" cut — a
 *  null bias is "not classified yet", and it must not be lumped into NEUTRAL,
 *  because neutral is a measurement and absence is not. */
export function filterStocksByBias(rows: StockOverviewRow[], filter: StockBiasFilter): StockOverviewRow[] {
  switch (filter) {
    case "CLASSIFIED":
      return rows.filter((row) => row.rsi_bias !== null);
    case "BULLISH":
      return rows.filter((row) => row.rsi_bias === "BULLISH");
    case "BEARISH":
      return rows.filter((row) => row.rsi_bias === "BEARISH");
    case "NEUTRAL":
      return rows.filter((row) => row.rsi_bias === "NEUTRAL");
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
