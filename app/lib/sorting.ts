/**
 * Sorting, server-side and pure.
 *
 * Two tables sort: today's memberships and the F&O universe. Both are sorted
 * in the page (after filtering, before rendering), never in SQL — the rows are
 * already in memory, and one comparator here serves the page, the tests and
 * any future view of the same rows.
 *
 * Two rules the comparators enforce:
 *
 * 1. **Nulls last, in both directions.** A dash means "the pipeline has not
 *    computed this yet" — flipping the arrow must never bury the rows that
 *    have real readings under the rows that have none.
 * 2. **Ties keep the source order.** `Array.prototype.sort` is stable, and the
 *    sources already order rows meaningfully (qualifiers first, then score), so
 *    equal keys under a chosen sort keep that meaning instead of an arbitrary
 *    one.
 *
 * The "column" strings are the URL parameter values; they match the table
 * prop names so a header can hand its own key to `hrefFor` without a lookup.
 */
import { SECTOR_LABELS, sectorFor } from "./sector-map";
import type { RsiBias, StockOverviewRow, WatchlistRow, WatchlistState } from "./types";

export type SortDir = "asc" | "desc";

/** The shape a sortable table takes when the page wires it: the active column
 *  (or null for the source's own order), the direction, and the link builder
 *  for a column's header — which the page closes over every other parameter. */
export type SortWiring = {
  key: string | null;
  dir: SortDir;
  hrefFor: (column: string) => string;
};

export const MEMBER_SORT_KEYS = [
  "symbol",
  "sector",
  "state",
  "score",
  "price",
  "vwap",
  "ema",
  "rvol",
  "rsi",
  "high",
  "candle",
] as const;
export type MemberSortKey = (typeof MEMBER_SORT_KEYS)[number];

export const STOCK_SORT_KEYS = [
  "symbol",
  "sector",
  "price",
  "change",
  "pe",
  "h52",
  "l52",
  "ath",
  "atl",
  "listed",
  "mcap",
  "rsid",
  "rsiw",
  "side",
] as const;
export type StockSortKey = (typeof STOCK_SORT_KEYS)[number];

/** Which direction a column means when first clicked. Numbers descend (the
 *  biggest first) unless small is the interesting end — an RSI sort is the dip
 *  hunt, so it ascends. Text ascends; dates descend (newest first). */
const MEMBER_DEFAULT_DIR: Record<MemberSortKey, SortDir> = {
  symbol: "asc",
  sector: "asc",
  state: "asc",
  score: "desc",
  price: "desc",
  vwap: "desc",
  ema: "desc",
  rvol: "desc",
  rsi: "asc",
  high: "desc",
  candle: "desc",
};

const STOCK_DEFAULT_DIR: Record<StockSortKey, SortDir> = {
  symbol: "asc",
  sector: "asc",
  price: "desc",
  change: "desc",
  pe: "asc",
  h52: "desc",
  l52: "desc",
  ath: "desc",
  atl: "desc",
  listed: "desc",
  mcap: "desc",
  rsid: "asc",
  rsiw: "asc",
  side: "asc",
};

/** The state order from healthy to degraded: the scanner's own pipeline order,
 *  so "ascending state" reads as the day's story. */
const STATE_ORDER: Record<WatchlistState, number> = {
  ACTIVE: 0,
  REQUALIFIED: 1,
  WARMING_UP: 2,
  WEAKENING: 3,
  DATA_STALE: 4,
  REMOVED: 5,
};

/** The operator's reason for having the page open, first: entry candidates,
 *  then the band, then the overbought tail. */
const BIAS_ORDER: Record<Exclude<RsiBias, null>, number> = {
  ENTRY: 0,
  NEUTRAL: 1,
  OVERBOUGHT: 2,
};

function first(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (Array.isArray(value) && typeof value[0] === "string") return value[0];
  return null;
}

export function parseMemberSort(value: unknown): MemberSortKey | null {
  const raw = first(value)?.toLowerCase();
  return (MEMBER_SORT_KEYS as readonly string[]).includes(raw ?? "") ? (raw as MemberSortKey) : null;
}

export function parseStockSort(value: unknown): StockSortKey | null {
  const raw = first(value)?.toLowerCase();
  return (STOCK_SORT_KEYS as readonly string[]).includes(raw ?? "") ? (raw as StockSortKey) : null;
}

export function parseSortDir(value: unknown, fallback: SortDir): SortDir {
  const raw = first(value)?.toLowerCase();
  return raw === "asc" || raw === "desc" ? raw : fallback;
}

export function memberSortDir(key: MemberSortKey): SortDir {
  return MEMBER_DEFAULT_DIR[key];
}

export function stockSortDir(key: StockSortKey): SortDir {
  return STOCK_DEFAULT_DIR[key];
}

/** What a header click does: same column flips, a different column switches to
 *  it at its own default direction. Pure, so the page and the tests agree. */
export function toggledDir(current: SortDir, sameColumn: boolean, fallback: SortDir): SortDir {
  if (!sameColumn) return fallback;
  return current === "asc" ? "desc" : "asc";
}

function byNumber<T>(dir: SortDir, pick: (row: T) => number | null): (a: T, b: T) => number {
  return (a, b) => {
    const x = pick(a);
    const y = pick(b);
    if (x === null && y === null) return 0;
    if (x === null) return 1; // nulls last in both directions
    if (y === null) return -1;
    return dir === "asc" ? x - y : y - x;
  };
}

function byText<T>(dir: SortDir, pick: (row: T) => string | null): (a: T, b: T) => number {
  return (a, b) => {
    const x = pick(a);
    const y = pick(b);
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    const order = x.localeCompare(y, "en", { sensitivity: "base" });
    return dir === "asc" ? order : -order;
  };
}

/** The chip's own label, so the sector sort groups rows the way the column
 *  reads. Unmapped symbols sort last — "not in a tracked index" is not a
 *  sector named A. */
function sectorLabelOf(symbol: string | null | undefined): string | null {
  const key = sectorFor(symbol);
  return key ? SECTOR_LABELS[key] : null;
}

/** The universe table's day change — the two-number formula its Chg % cell
 *  prints; a missing or zero reference close is "no reading", not 0%. */
export function stockDayChange(row: StockOverviewRow): number | null {
  if (row.close === null || row.previous_close === null || row.previous_close === 0) return null;
  return ((row.close - row.previous_close) / row.previous_close) * 100;
}

function memberSymbol(row: WatchlistRow): string | null {
  return row.symbol ?? row.trading_symbol ?? null;
}

export function compareMembers(dir: SortDir, key: MemberSortKey): (a: WatchlistRow, b: WatchlistRow) => number {
  switch (key) {
    case "symbol":
      return byText(dir, memberSymbol);
    case "sector":
      return byText(dir, (row) => sectorLabelOf(row.symbol ?? row.trading_symbol));
    case "state":
      return byNumber(dir, (row) => STATE_ORDER[row.current_state]);
    case "score":
      return byNumber(dir, (row) => row.latest_score);
    case "price":
      return byNumber(dir, (row) => row.latest_price);
    case "vwap":
      return byNumber(dir, (row) => row.distance_from_vwap_pct);
    case "ema":
      return byNumber(dir, (row) => row.ema_spread_pct);
    case "rvol":
      return byNumber(dir, (row) => row.relative_volume);
    case "rsi":
      return byNumber(dir, (row) => row.rsi_5m);
    case "high":
      return byNumber(dir, (row) => row.distance_from_high_pct);
    case "candle":
      return byText(dir, (row) => row.latest_candle_at);
  }
}

export function compareStocks(dir: SortDir, key: StockSortKey): (a: StockOverviewRow, b: StockOverviewRow) => number {
  switch (key) {
    case "symbol":
      return byText(dir, (row) => row.symbol);
    case "sector":
      return byText(dir, (row) => sectorLabelOf(row.symbol));
    case "price":
      return byNumber(dir, (row) => row.close);
    case "change":
      return byNumber(dir, stockDayChange);
    case "pe":
      return byNumber(dir, (row) => row.pe_ratio);
    case "h52":
      return byNumber(dir, (row) => row.high_52w);
    case "l52":
      return byNumber(dir, (row) => row.low_52w);
    case "ath":
      return byNumber(dir, (row) => row.ath);
    case "atl":
      return byNumber(dir, (row) => row.atl);
    case "listed":
      return byText(dir, (row) => row.listing_date);
    case "mcap":
      return byNumber(dir, (row) => row.market_cap_cr);
    case "rsid":
      return byNumber(dir, (row) => row.daily_rsi);
    case "rsiw":
      return byNumber(dir, (row) => row.weekly_rsi);
    case "side":
      return byNumber(dir, (row) => (row.rsi_bias === null ? null : BIAS_ORDER[row.rsi_bias]));
  }
}
