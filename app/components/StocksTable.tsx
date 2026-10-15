import Link from "next/link";
import { Sparkline } from "./Sparkline";
import { RsiBiasBadge, rsiBandTone } from "./badges";
import { Empty } from "./ui";
import { crores, orDash, pct, price, rsi } from "@/lib/format";
import type { StockOverviewRow } from "@/lib/types";

/**
 * The universe directory: every stock the scanner considers, with the fundamentals
 * the daily/profile sync has attached and the two-timeframe RSI classification the
 * workspace alerts on.
 *
 * A dash here always means "that sync has not covered this column yet" — the
 * LEFT JOINs in the source keep an unsynced stock in the list, because a stock
 * missing from a directory of 212 is indistinguishable from one that was deleted.
 */
export function StocksTable({
  rows,
  sparklines,
}: {
  rows: StockOverviewRow[];
  /** The newest daily closes per instrument, oldest → newest. The Trend column
   *  renders only when the caller could read the series. */
  sparklines?: Record<number, number[]>;
}) {
  if (rows.length === 0) {
    return <Empty>The universe is empty — no instruments are flagged for the configured scan set.</Empty>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line">
            <th className="th">Symbol</th>
            <th className="th text-right">Price</th>
            <th className="th text-right">Chg %</th>
            {sparklines ? (
              <th className="th hidden lg:table-cell" title="Last 15 completed daily closes, oldest → newest">
                Trend
              </th>
            ) : null}
            <th className="th hidden text-right xl:table-cell">P/E</th>
            <th className="th hidden text-right xl:table-cell">52W high</th>
            <th className="th hidden text-right xl:table-cell">52W low</th>
            <th className="th hidden text-right xl:table-cell">ATH</th>
            <th className="th hidden text-right xl:table-cell">ATL</th>
            <th className="th hidden text-right xl:table-cell">Listed</th>
            <th className="th hidden text-right xl:table-cell">Mkt cap</th>
            <th className="th text-right" title="Wilder RSI-14 from the last completed daily bar">
              RSI D
            </th>
            <th className="th text-right" title="Wilder RSI-14 resampled to ISO weeks">
              RSI W
            </th>
            <th className="th" title="Both timeframes below 40 is an entry; both above 60 is overbought">
              Side
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const change = dayChange(row);
            return (
              <tr key={row.instrument_id} className="row-hover border-b border-line last:border-0">
                <td className="td">
                  <Link href={`/instrument/${encodeURIComponent(row.symbol)}`} className="num font-semibold link">
                    {row.symbol}
                  </Link>
                  <div className="mt-0.5 max-w-[220px] truncate text-[11px] text-muted">{orDash(row.company_name)}</div>
                </td>
                <td className="td num text-right">
                  {price(row.close)}
                  <div className="num text-[11px] text-muted">{row.last_bar_date ? orDash(row.last_bar_date) : ""}</div>
                </td>
                <td className={`td num text-right ${changeTone(change)}`}>{pct(change)}</td>
                {sparklines ? (
                  <td className="td hidden lg:table-cell">
                    <Sparkline
                      values={sparklines[row.instrument_id]}
                      width={120}
                      missing={`No daily closes synced for ${row.symbol}`}
                    />
                  </td>
                ) : null}
                <td className="td hidden num text-right xl:table-cell">{row.pe_ratio === null ? "—" : row.pe_ratio.toFixed(1)}</td>
                <td className="td hidden num text-right xl:table-cell">{price(row.high_52w)}</td>
                <td className="td hidden num text-right xl:table-cell">{price(row.low_52w)}</td>
                <td className="td hidden num text-right xl:table-cell">{price(row.ath)}</td>
                <td className="td hidden num text-right xl:table-cell">{price(row.atl)}</td>
                <td className="td hidden num text-right xl:table-cell" title={row.listing_date ?? undefined}>
                  {row.listing_date ? row.listing_date.slice(0, 4) : "—"}
                </td>
                <td className="td hidden num text-right xl:table-cell">{crores(row.market_cap_cr)}</td>
                <td className={`td num text-right ${rsiBandTone(row.daily_rsi)}`}>{rsi(row.daily_rsi)}</td>
                <td className={`td num text-right ${rsiBandTone(row.weekly_rsi)}`}>{rsi(row.weekly_rsi)}</td>
                <td className="td">
                  <RsiBiasBadge bias={row.rsi_bias} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Change from the previous daily close — the same adjusted series the price cell
 *  prints, so the two numbers can never disagree. */
function dayChange(row: StockOverviewRow): number | null {
  if (row.close === null || row.previous_close === null || row.previous_close === 0) return null;
  return ((row.close - row.previous_close) / row.previous_close) * 100;
}

function changeTone(change: number | null): string {
  if (change === null) return "text-muted";
  if (change > 0) return "text-positive";
  if (change < 0) return "text-negative";
  return "text-ink-2";
}
