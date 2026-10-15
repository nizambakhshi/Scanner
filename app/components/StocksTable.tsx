import Link from "next/link";
import { SectorChip } from "./SectorChip";
import { SortHeader } from "./SortHeader";
import { Sparkline } from "./Sparkline";
import { RsiBiasBadge, rsiBandTone } from "./badges";
import { Empty } from "./ui";
import { crores, orDash, pct, price, rsi } from "@/lib/format";
import type { SectorReads } from "@/lib/sector-reads";
import { sectorFor } from "@/lib/sector-map";
import { stockDayChange, type SortWiring } from "@/lib/sorting";
import type { StockOverviewRow } from "@/lib/types";

/**
 * The universe directory: every stock the scanner considers, with the fundamentals
 * the daily/profile sync has attached and the two-timeframe RSI classification the
 * workspace alerts on.
 *
 * A dash here always means "that sync has not covered this column yet" — the
 * LEFT JOINs in the source keep an unsynced stock in the list, because a stock
 * missing from a directory of 212 is indistinguishable from one that was deleted.
 *
 * When the page passes `sort`, the column headers become toggle links; the page
 * has then already sorted the rows with the matching comparator.
 */
export function StocksTable({
  rows,
  sparklines,
  sectorReads,
  sort,
}: {
  rows: StockOverviewRow[];
  /** The newest daily closes per instrument, oldest → newest. The Trend column
   *  renders only when the caller could read the series. */
  sparklines?: Record<number, number[]>;
  /** The sector indices' readings for first paint; the chips then keep
   *  themselves current off the shared poller. */
  sectorReads?: SectorReads;
  /** Present only when the page wired sorting; headers then toggle. */
  sort?: SortWiring;
}) {
  if (rows.length === 0) {
    return <Empty>The universe is empty — no instruments are flagged for the configured scan set.</Empty>;
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
            <SortHeader label="Price" column="price" sort={sort} className="th text-right" />
            <SortHeader label="Chg %" column="change" sort={sort} className="th text-right" />
            {sparklines ? (
              <th className="th hidden lg:table-cell" title="Last 15 completed daily closes, oldest → newest">
                Trend
              </th>
            ) : null}
            <SortHeader label="P/E" column="pe" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="52W high" column="h52" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="52W low" column="l52" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="ATH" column="ath" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="ATL" column="atl" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="Listed" column="listed" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader label="Mkt cap" column="mcap" sort={sort} className="th hidden text-right xl:table-cell" />
            <SortHeader
              label="RSI D"
              column="rsid"
              sort={sort}
              className="th text-right"
              title="Wilder RSI-14 from the last completed daily bar"
            />
            <SortHeader
              label="RSI W"
              column="rsiw"
              sort={sort}
              className="th text-right"
              title="Wilder RSI-14 resampled to ISO weeks"
            />
            <SortHeader
              label="Side"
              column="side"
              sort={sort}
              className="th"
              title="Both timeframes below 40 is an entry; both above 60 is overbought"
            />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const change = stockDayChange(row);
            const sector = sectorFor(row.symbol);
            return (
              <tr key={row.instrument_id} className="row-hover border-b border-line last:border-0">
                <td className="td">
                  <Link href={`/instrument/${encodeURIComponent(row.symbol)}`} className="num font-semibold link">
                    {row.symbol}
                  </Link>
                  <div className="mt-0.5 max-w-[220px] truncate text-[11px] text-muted">{orDash(row.company_name)}</div>
                </td>
                <td className="td hidden md:table-cell">
                  <SectorChip sector={sector} initial={sector ? (sectorReads?.[sector] ?? null) : null} />
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

/** The Chg % cell's own tone. The change itself comes from `stockDayChange`
 *  (lib/sorting) — one formula for the number and the sort behind it. */
function changeTone(change: number | null): string {
  if (change === null) return "text-muted";
  if (change > 0) return "text-positive";
  if (change < 0) return "text-negative";
  return "text-ink-2";
}
