import { Panel, ErrorNote, PageHeading } from "@/components/ui";
import { StocksTable } from "@/components/StocksTable";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * The stocks directory: every instrument in the scanner's configured universe, with
 * the fundamentals and the two-timeframe RSI attached.
 *
 * The universe band on this page follows the newest saved config (the same
 * `is_fno` / `is_nifty500` switch the worker applies), so the list is exactly what
 * the scanner scans — not a static NIFTY500 copy that would silently disagree with
 * it. Prices come from the synced daily bars, which make this page readable outside
 * market hours, when no intraday candle exists.
 */
export default async function StocksPage() {
  const stocks = await panel(() => source().stocksOverview());
  const rows = ok(stocks) ? stocks.data : [];

  const classified = rows.filter((row) => row.rsi_bias !== null).length;
  const bullish = rows.filter((row) => row.rsi_bias === "BULLISH").length;
  const bearish = rows.filter((row) => row.rsi_bias === "BEARISH").length;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Stocks"
        subtitle={
          <>
            Every stock in the scanner&rsquo;s universe, with the daily-bar fundamentals and the RSI classification the
            alerts run on: daily and weekly RSI-14 both above 60 is bullish, both below 40 is bearish, anything else is
            neutral.
          </>
        }
      />

      <div className="flex flex-wrap gap-2 text-[12px]">
        <Tally label="In view" value={rows.length} />
        <Tally label="RSI classified" value={classified} tone={classified > 0 ? "text-ink-2" : "text-amber"} />
        <Tally label="Bullish" value={bullish} tone="text-positive" />
        <Tally label="Bearish" value={bearish} tone="text-negative" />
      </div>

      <Panel title="Universe" hint={ok(stocks) ? `read from the ${source().name} source` : undefined}>
        {ok(stocks) ? <StocksTable rows={rows} /> : <ErrorNote title="Stocks directory unavailable" detail={stocks.error} />}
      </Panel>

      {ok(stocks) && rows.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          Price and change% come from the last two synced daily bars, and the RSI readings from the daily/weekly close
          series (Wilder RSI-14). A dash in a cell means that sync has not covered the symbol yet, not that the value is
          zero. Open a symbol for its last seven daily and weekly RSI readings.
        </p>
      ) : null}
    </div>
  );
}

function Tally({ label, value, tone = "text-ink-2" }: { label: string; value: number; tone?: string }) {
  return (
    <span className="chip border-line bg-surface text-muted">
      {label} <span className={`num font-bold ${tone}`}>{value}</span>
    </span>
  );
}
