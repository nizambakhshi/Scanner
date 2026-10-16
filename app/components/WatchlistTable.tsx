import Link from "next/link";
import { SectorChip } from "./SectorChip";
import { SimChip } from "./SimulatedBanner";
import { SortHeader } from "./SortHeader";
import { Sparkline } from "./Sparkline";
import { RsiStateBadge, StateBadge, rsiBandTone } from "./badges";
import { Empty } from "./ui";
import { clock, dateTime, multiple, orDash, pct, price, rsi, score } from "@/lib/format";
import type { SectorReads } from "@/lib/sector-reads";
import { sectorFor } from "@/lib/sector-map";
import type { SortWiring } from "@/lib/sorting";
import type { WatchlistRow } from "@/lib/types";

/**
 * The watchlist, which is the product.
 *
 * Columns are ordered the way an operator reads: what is it, which sector is it
 * in, is it still qualifying, how strong is it, and what the three conditions
 * actually measured. A `null` here always means "the pipeline has not computed
 * this yet" — a warming-up symbol genuinely has no EMA20, so it renders as a
 * dash, never as 0, because 0 would read as "failed the test" instead of "not
 * tested".
 *
 * When the page passes `sort`, the column headers become toggle links (the
 * rows arrive already sorted — the page owns the comparator); without it the
 * headers are plain labels and the table keeps the source's own order.
 */
export function WatchlistTable({
  rows,
  showSymbolLink = true,
  sparklines,
  sectorReads,
  sort,
}: {
  rows: WatchlistRow[];
  showSymbolLink?: boolean;
  /** Today's 5-minute closes per instrument, oldest → newest. The Trend column
   *  renders only when the caller could read the series — a column of dashes
   *  would be noise, and a fabricated line would be worse. */
  sparklines?: Record<number, number[]>;
  /** The sector indices' readings for first paint; the chips then keep
   *  themselves current off the shared poller. */
  sectorReads?: SectorReads;
  /** Present only when the page wired sorting; headers then toggle. */
  sort?: SortWiring;
}) {
  if (rows.length === 0) {
    return <Empty>Nothing qualifies yet. That is a real answer, not a loading state.</Empty>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line">
            <SortHeader label="Symbol" column="symbol" sort={sort} className="th" />
            <SortHeader
              label="Sector"
              column="sector"
              sort={sort}
              className="th hidden md:table-cell"
              title="The NIFTY sector index this symbol belongs to — the chip is green while that index is up on the day, red while down"
            />
            {sparklines ? (
              <th className="th hidden lg:table-cell" title="Close of each traded 5-minute bucket today, oldest → newest">
                Trend
              </th>
            ) : null}
            <SortHeader label="State" column="state" sort={sort} className="th" />
            <SortHeader label="Score" column="score" sort={sort} className="th text-right" />
            <SortHeader label="Price" column="price" sort={sort} className="th text-right" />
            <SortHeader label="vs VWAP" column="vwap" sort={sort} className="th text-right" />
            <SortHeader label="EMA spread" column="ema" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="Rel vol" column="rvol" sort={sort} className="th hidden text-right md:table-cell" />
            <SortHeader
              label="RSI 5m"
              column="rsi"
              sort={sort}
              className="th text-right"
              title="Wilder RSI-14 on today's 5-minute closes, judged against the live trend — the reading the entry alert fires on"
            />
            <SortHeader label="vs high" column="high" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="Candle" column="candle" sort={sort} className="th" />
            <th className="th">Why</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const href = row.symbol ? `/instrument/${encodeURIComponent(row.symbol)}` : null;
            const qualifying = row.current_state === "ACTIVE" || row.current_state === "REQUALIFIED";
            const sector = sectorFor(row.symbol);
            return (
              <tr key={row.watchlist_id} className="row-hover border-b border-line last:border-0">
                <td className="td">
                  {href && showSymbolLink ? (
                    <Link href={href} className="num font-semibold link">
                      {row.symbol ?? row.trading_symbol ?? row.instrument_id}
                    </Link>
                  ) : (
                    <span className="num font-semibold">{row.symbol ?? row.trading_symbol ?? row.instrument_id}</span>
                  )}
                  <div className="mt-0.5 max-w-[190px] truncate text-[11px] text-muted">
                    {orDash(row.company_name)} · {orDash(row.exchange)}
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <SimChip value={row.is_simulated} />
                    {index === 0 && row.episode_count > 1 ? (
                      <span className="text-[11px] text-muted">{row.episode_count} episodes today</span>
                    ) : null}
                  </div>
                </td>
                <td className="td hidden md:table-cell">
                  <SectorChip sector={sector} initial={sector ? (sectorReads?.[sector] ?? null) : null} />
                </td>
                {sparklines ? (
                  <td className="td hidden lg:table-cell">
                    <Sparkline
                      values={sparklines[row.instrument_id]}
                      missing={`No 5-minute closes today for ${row.symbol ?? row.trading_symbol ?? "this symbol"}`}
                    />
                  </td>
                ) : null}
                <td className="td">
                  <StateBadge state={row.current_state} />
                  <div className="num mt-1 text-[11px] text-muted">
                    {row.initial_entry_at ? `in at ${clock(row.initial_entry_at)}` : "—"}
                    {row.removal_reason ? <span className="ml-1 text-negative">· {row.removal_reason}</span> : null}
                  </div>
                  {row.consecutive_weak_candles > 0 ? (
                    <div className="text-[11px] text-amber">{row.consecutive_weak_candles} weak candle(s)</div>
                  ) : null}
                </td>
                <td className="td num text-right font-semibold">{score(row.latest_score)}</td>
                <td className="td num text-right">
                  {price(row.latest_price)}
                  <div className="num text-[11px] text-muted">
                    {row.initial_entry_price === null ? "" : `entry ${price(row.initial_entry_price)}`}
                  </div>
                </td>
                <td className={`td num text-right ${tone(row.distance_from_vwap_pct)}`}>
                  {pct(row.distance_from_vwap_pct)}
                  <div className="num text-[11px] text-muted">{row.vwap === null ? "" : `vwap ${price(row.vwap)}`}</div>
                </td>
                <td className={`td hidden num text-right xl:table-cell ${tone(row.ema_spread_pct)}`}>
                  {pct(row.ema_spread_pct)}
                  <div className="num text-[11px] text-muted">
                    {row.ema9 === null || row.ema20 === null ? "" : `${price(row.ema9)} / ${price(row.ema20)}`}
                  </div>
                </td>
                <td className="td hidden num text-right md:table-cell">
                  <span className={row.relative_volume !== null && row.relative_volume >= 1.5 ? "text-positive" : ""}>
                    {multiple(row.relative_volume)}
                  </span>
                </td>
                <td className={`td num text-right ${rsiBandTone(row.rsi_5m)}`}>
                  {row.rsi_5m === null ? (
                    <span className="text-muted" title="No five-minute RSI reading on this snapshot — the session is younger than the warmup or the worker restarted">
                      —
                    </span>
                  ) : (
                    rsi(row.rsi_5m)
                  )}
                  <div className="mt-0.5 flex justify-end">
                    <RsiStateBadge state={row.rsi_state} />
                  </div>
                </td>
                <td className={`td hidden num text-right xl:table-cell ${tone(row.distance_from_high_pct)}`}>
                  {pct(row.distance_from_high_pct)}
                  <div className="num text-[11px] text-muted">{row.session_high === null ? "" : `H ${price(row.session_high)}`}</div>
                </td>
                <td className="td">
                  <div className="num text-[12px]">{row.latest_candle_at ? clock(row.latest_candle_at) : "—"}</div>
                  <div className="mt-0.5 flex items-center gap-1">
                    <DataQualityChip status={row.data_quality} />
                    {row.warming_up ? <span className="text-[11px] text-amber">warming up</span> : null}
                  </div>
                </td>
                <td className="td max-w-[240px]">
                  <div className="flex flex-wrap gap-1">
                    {qualifying
                      ? row.reasons.map((reason) => <span key={reason} className="chip border-positive/30 bg-positive/5 text-[10px] text-positive">{short(reason)}</span>)
                      : null}
                    {row.failed_reasons.map((reason) => (
                      <span key={reason} className="chip border-negative/30 bg-negative/5 text-[10px] text-negative">
                        {short(reason)}
                      </span>
                    ))}
                    {row.reasons.length === 0 && row.failed_reasons.length === 0 ? (
                      <span className="text-[11px] text-muted">{dateTime(row.latest_state_change_at)}</span>
                    ) : null}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined) return "text-muted";
  if (value > 0) return "text-positive";
  if (value < 0) return "text-negative";
  return "text-ink-2";
}

/** Reason codes come from the scanner verbatim (`CLOSE_ABOVE_VWAP`); the table has
 *  to stay scannable, so the chip shortens and the tooltip keeps the exact code. */
function short(code: string): string {
  return code
    .replace("_ABOVE_", ">")
    .replace("_BELOW_", "<")
    .replace(/_/g, " ")
    .toLowerCase();
}

export function DataQualityChip({ status }: { status: string | null }) {
  if (!status || status === "OK") return <span className="chip border-line-2 bg-surface-2 text-[10px] text-muted">OK</span>;
  const bad = status === "STALE" || status === "MISSING";
  return (
    <span className={`chip text-[10px] ${bad ? "border-negative/40 bg-negative/10 text-negative" : "border-amber/40 bg-amber/10 text-amber"}`}>
      {status}
    </span>
  );
}
