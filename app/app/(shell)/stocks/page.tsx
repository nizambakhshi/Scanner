import { Panel, ErrorNote, PageHeading, FilterChip, Empty } from "@/components/ui";
import { StocksTable } from "@/components/StocksTable";
import { filterStocksByBias, parseStockBiasFilter, type StockBiasFilter } from "@/lib/filters";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/** What an empty result under each filter means — written per filter so the
 *  message never confuses "nothing in this cut" with "the universe is empty". */
const FILTER_EMPTY: Record<Exclude<StockBiasFilter, "ALL">, string> = {
  CLASSIFIED: "No classified stocks yet — the daily/weekly sync has not covered any symbol",
  BULLISH: "No bullish stocks right now",
  BEARISH: "No bearish stocks right now",
  NEUTRAL: "No neutral stocks right now",
};

/**
 * The stocks directory: every instrument in the scanner's configured universe, with
 * the fundamentals and the two-timeframe RSI attached.
 *
 * The universe band on this page follows the newest saved config (the same
 * `is_fno` / `is_nifty500` switch the worker applies), so the list is exactly what
 * the scanner scans — not a static NIFTY500 copy that would silently disagree with
 * it. Prices come from the synced daily bars, which make this page readable outside
 * market hours, when no intraday candle exists.
 *
 * The tally chips above the table are the table's filters: each chip's count is a
 * statement about the full universe, and clicking one narrows the table (the cut
 * lives in `?bias=`, so it is shareable and the back button works).
 */
export default async function StocksPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const bias = parseStockBiasFilter(params.bias);

  const stocks = await panel(() => source().stocksOverview());
  const sparks = await panel(() => source().dailySparklines());
  const rows = ok(stocks) ? stocks.data : [];
  const visible = filterStocksByBias(rows, bias);
  // A failed series read drops the Trend column entirely — a column of dashes
  // would suggest the data was read and found empty.
  const sparklines = ok(sparks) ? sparks.data : undefined;

  const classified = rows.filter((row) => row.rsi_bias !== null).length;
  const bullish = rows.filter((row) => row.rsi_bias === "BULLISH").length;
  const bearish = rows.filter((row) => row.rsi_bias === "BEARISH").length;
  const neutral = rows.filter((row) => row.rsi_bias === "NEUTRAL").length;

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
        <FilterChip href="/stocks" label="In view" value={rows.length} active={bias === "ALL"} />
        <FilterChip
          href="/stocks?bias=CLASSIFIED"
          label="RSI classified"
          value={classified}
          tone={classified > 0 ? "text-ink-2" : "text-amber"}
          active={bias === "CLASSIFIED"}
        />
        <FilterChip href="/stocks?bias=BULLISH" label="Bullish" value={bullish} tone="text-positive" active={bias === "BULLISH"} />
        <FilterChip href="/stocks?bias=BEARISH" label="Bearish" value={bearish} tone="text-negative" active={bias === "BEARISH"} />
        <FilterChip href="/stocks?bias=NEUTRAL" label="Neutral" value={neutral} tone="text-muted" active={bias === "NEUTRAL"} />
      </div>

      <Panel
        title="Universe"
        hint={
          ok(stocks)
            ? `read from the ${source().name} source${bias !== "ALL" ? ` · showing ${visible.length}` : ""}`
            : undefined
        }
      >
        {ok(stocks) ? (
          rows.length > 0 && visible.length === 0 && bias !== "ALL" ? (
            <Empty>{FILTER_EMPTY[bias]} — the counts above are of the full universe.</Empty>
          ) : (
            <StocksTable rows={visible} sparklines={sparklines} />
          )
        ) : (
          <ErrorNote title="Stocks directory unavailable" detail={stocks.error} />
        )}
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
