import Link from "next/link";
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
  ENTRY: "No stock has both timeframes below 40 right now",
  OVERBOUGHT: "No stock has both timeframes above 60 right now",
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
  const entry = rows.filter((row) => row.rsi_bias === "ENTRY").length;
  const overbought = rows.filter((row) => row.rsi_bias === "OVERBOUGHT").length;
  const neutral = rows.filter((row) => row.rsi_bias === "NEUTRAL").length;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Stocks"
        subtitle={
          <>
            Every stock in the scanner&rsquo;s universe, with the daily-bar fundamentals and the RSI classification the
            alerts run on: daily and weekly RSI-14 both below 40 is an <strong className="font-semibold text-ink">entry</strong>,
            both above 60 is <strong className="font-semibold text-ink">overbought</strong>, anything else is neutral.
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
        <FilterChip href="/stocks?bias=ENTRY" label="Entry" value={entry} tone="text-positive" active={bias === "ENTRY"} />
        <FilterChip
          href="/stocks?bias=OVERBOUGHT"
          label="Overbought"
          value={overbought}
          tone="text-negative"
          active={bias === "OVERBOUGHT"}
        />
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
          series (Wilder RSI-14) — the nightly pair, which is why an entry here means &ldquo;cheap on both timeframes at
          yesterday&rsquo;s close&rdquo;. Today&rsquo;s five-minute RSI, the one the entry alert fires on, is on the{" "}
          <Link href="/watchlist" className="link">
            board
          </Link>
          . A dash in a cell means that sync has not covered the symbol yet, not that the value is zero. Open a symbol
          for its last seven daily and weekly RSI readings.
        </p>
      ) : null}
    </div>
  );
}
