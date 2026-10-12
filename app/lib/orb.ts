/**
 * Opening Range Breakout — the derivation the /strategy page renders.
 *
 * The strategy this page follows (read-only: the scanner never places orders):
 * mark the high and low of the first five minutes (9:15–9:20 IST). A 5-minute
 * close beyond that range after 9:20 is the signal; the stop is the opposite end
 * of the range and the target is 1–2× the range width. The filters are price on
 * the right side of session VWAP, the 9/20 EMA agreeing with the direction, and
 * the breakout candle trading at least 1.5× the average 5-minute volume of the
 * session so far.
 *
 * Everything here is a pure function of the row the source returned, so the
 * status and level arithmetic is unit-tested without a database — same split as
 * the rest of the app (`format.ts`, `rows.ts`).
 */

import type { OrbDirection, OrbRow, OrbStatus, OrbView } from "./types";

/** The volume filter from the strategy write-up: the breakout candle must trade
 *  at least this many times the session's average 5-minute volume so far. */
export const ORB_VOLUME_MULTIPLE = 1.5;

export function deriveOrb(row: OrbRow): OrbView {
  const base: OrbView = {
    row,
    status: "NO_DATA",
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
  };
  if (row.or_high === null || row.or_low === null) return base;

  const width = row.or_high - row.or_low;
  const width_pct = row.or_low > 0 ? (width / row.or_low) * 100 : null;
  const direction = firstDirection(row);
  const status: OrbStatus =
    direction === "UP"
      ? "BROKE_UP"
      : direction === "DOWN"
        ? "BROKE_DOWN"
        : row.latest_at === null || isRangeOnly(row)
          ? "OPENING"
          : "INSIDE";

  const latest = row.latest_close;
  const retreated =
    direction === "UP"
      ? latest !== null && latest <= row.or_high
      : direction === "DOWN"
        ? latest !== null && latest >= row.or_low
        : false;

  const levels =
    direction === "UP"
      ? { stop: row.or_low, target1: row.or_high + width, target2: row.or_high + 2 * width }
      : direction === "DOWN"
        ? { stop: row.or_high, target1: row.or_low - width, target2: row.or_low - 2 * width }
        : { stop: null, target1: null, target2: null };

  const vwap_side =
    latest === null || row.session_vwap === null
      ? null
      : latest > row.session_vwap
        ? ("above" as const)
        : latest < row.session_vwap
          ? ("below" as const)
          : null;

  const ratio = direction === "UP" ? row.bu_ratio : direction === "DOWN" ? row.bd_ratio : null;
  const volume_agrees = direction === null || ratio === null ? null : ratio >= ORB_VOLUME_MULTIPLE;

  const vwap_agrees =
    direction === null || vwap_side === null ? null : direction === "UP" ? vwap_side === "above" : vwap_side === "below";

  const ema_agrees =
    direction === null || row.ema9 === null || row.ema20 === null || row.ema9 === row.ema20
      ? null
      : direction === "UP"
        ? row.ema9 > row.ema20
        : row.ema9 < row.ema20;

  return {
    ...base,
    status,
    direction,
    retreated,
    width,
    width_pct,
    ...levels,
    vwap_side,
    vwap_agrees,
    volume_agrees,
    ema_agrees,
    confirmed: direction !== null && vwap_agrees === true && volume_agrees === true && ema_agrees === true,
  };
}

/** The first breakout — earliest 5-minute close to leave the range, either side.
 *  Timestamps are the same `+05:30` ISO shape, so string order is time order. */
function firstDirection(row: OrbRow): OrbDirection | null {
  if (row.bu_at !== null && row.bd_at !== null) return row.bu_at <= row.bd_at ? "UP" : "DOWN";
  if (row.bu_at !== null) return "UP";
  if (row.bd_at !== null) return "DOWN";
  return null;
}

/** True when the newest bucket is still the opening-range candle itself — the
 *  session has not produced a single post-range 5-minute close yet. The range
 *  window closes at 9:20 IST, and +05:30 has no DST, so the comparison is exact. */
function isRangeOnly(row: OrbRow): boolean {
  if (row.latest_at === null || row.session_date === null) return false;
  return row.latest_at <= `${row.session_date}T09:20:00+05:30`;
}

/** Breakouts first (by breakout time), then rows still in play by widest range,
 *  then rows whose session never got past the range, then no data. Ties by symbol. */
export function compareOrb(a: OrbView, b: OrbView): number {
  const rank = (view: OrbView): number =>
    view.status === "BROKE_UP" || view.status === "BROKE_DOWN" ? 0 : view.status === "INSIDE" ? 1 : view.status === "OPENING" ? 2 : 3;
  const ra = rank(a);
  const rb = rank(b);
  if (ra !== rb) return ra - rb;
  if (ra === 0) {
    const at = (view: OrbView) => (view.direction === "UP" ? view.row.bu_at : view.row.bd_at) ?? "";
    const [ta, tb] = [at(a), at(b)];
    if (ta !== tb) return ta < tb ? -1 : 1;
  }
  if (ra === 1) {
    const [wa, wb] = [a.width_pct ?? -1, b.width_pct ?? -1];
    if (wa !== wb) return wb - wa;
  }
  return a.row.symbol.localeCompare(b.row.symbol);
}
