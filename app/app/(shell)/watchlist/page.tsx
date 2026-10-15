import Link from "next/link";
import { Panel, Empty, ErrorNote, FilterChip, PageHeading } from "@/components/ui";
import { StageStrip } from "@/components/StageStrip";
import { StocksTable } from "@/components/StocksTable";
import { SignalsTable } from "@/components/SignalsTable";
import { WatchlistTable } from "@/components/WatchlistTable";
import {
  SIGNAL_TYPES,
  filterStocksByBias,
  filterWatchlistByState,
  parseSignalType,
  parseStockBiasFilter,
  parseWatchlistStateFilter,
  type StockBiasFilter,
  type WatchlistStateFilter,
} from "@/lib/filters";
import { humanise, longDate, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";
import { buildStages, removedCount, trackedCount, weakeningCount } from "@/lib/stages";
import type { SignalType } from "@/lib/types";

export const dynamic = "force-dynamic";

/** The feed is the day's evidence, not an archive — the cap keeps the page light
 *  on a heavy day, and the stage-4 tile still counts the full day from the
 *  session summary. */
const FEED_LIMIT = 150;

/** What an empty result under each members cut means, spelled per cut so the
 *  message never confuses "nothing tracked" with "nothing happened today". */
const STATE_EMPTY: Record<Exclude<WatchlistStateFilter, "ALL">, string> = {
  TRACKED: "Nothing is tracked right now — every symbol that qualified today has since weakened or been removed.",
  WEAKENING: "Nothing is weakening — every membership is either tracked or already removed.",
  REMOVED: "Nothing was removed today. The removal rule needs consecutive weak candles, so it stays quiet on a strong day.",
};

/** What an empty result under each universe cut means — same wording as the old
 *  /stocks page carried, because the counts above the table are still of the
 *  full universe. */
const BIAS_EMPTY: Record<Exclude<StockBiasFilter, "ALL">, string> = {
  CLASSIFIED: "No classified stocks yet — the daily/weekly sync has not covered any symbol",
  ENTRY: "No stock has both timeframes below 40 right now",
  OVERBOUGHT: "No stock has both timeframes above 60 right now",
  NEUTRAL: "No neutral stocks right now",
};

/**
 * The Watchlist — one page for the whole day, in the order the scanner walks
 * through it: the F&O universe it scans, what the initial scan discovered, the
 * memberships that qualified, the alerts raised, what is still being tracked,
 * and what was removed. The three older pages (/watchlist, /stocks, /signals)
 * were one story told three times; they now redirect here.
 *
 * "Today" is not a filter here, it is the page's premise: the session row from
 * `v_session_history` supplies the date, and the watchlist and feed are read
 * with it. If that row cannot be read, the rows are cut in code to their own
 * newest trading date — a past session must never masquerade as today's.
 * Yesterday lives on the Days page.
 */
export default async function WatchlistPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const state = parseWatchlistStateFilter(params.state);
  const bias = parseStockBiasFilter(params.bias);
  // An unknown type in the URL shows the whole feed, which is what a stale
  // bookmark asks for — same rule the old /signals page enforced.
  const type: SignalType | "" = parseSignalType(params.type) ?? "";

  // Wave one: the session anchor and every stage source, one read per panel so a
  // failed read breaks its own panel and nothing else.
  const [days, coverage, scan, stocks, sparks, dailySparks] = await Promise.all([
    panel(() => source().history(1)),
    panel(() => source().coverage()),
    panel(() => source().initialScan()),
    panel(() => source().stocksOverview()),
    panel(() => source().intradaySparklines()),
    panel(() => source().dailySparklines()),
  ]);
  const session = ok(days) ? days.data[0] ?? null : null;
  const today = session?.trading_date ?? null;

  // Wave two: today's memberships and feed, filtered at the data source when the
  // session date is known.
  const [watch, feed] = await Promise.all([
    panel(() => source().currentWatchlist(today)),
    panel(() => source().signals({ tradingDate: today, limit: FEED_LIMIT })),
  ]);
  const allMembers = ok(watch) ? watch.data : [];
  const allFeed = ok(feed) ? feed.data : [];

  // The belt to the source filter's braces: with no session row (or a database
  // the view answered too broadly for), cut to the newest date actually present
  // rather than trusting the query alone.
  const sessionDate = today ?? newestDate([...allMembers, ...allFeed]);
  const members = sessionDate ? allMembers.filter((row) => row.trading_date === sessionDate) : allMembers;
  const feedRows = sessionDate ? allFeed.filter((row) => row.trading_date === sessionDate) : allFeed;

  // Wave three: delivery rows are keyed by the signal ids the feed read returned.
  const deliveries = await panel(() => source().deliveries(feedRows.map((row) => row.id)));

  // ── the day's arithmetic ────────────────────────────────────────────────────
  const tracked = trackedCount(members);
  const weakening = weakeningCount(members);
  const removed = removedCount(members);
  const visibleMembers = filterWatchlistByState(members, state);

  const typesPresent = SIGNAL_TYPES.filter((candidate) => feedRows.some((row) => row.signal_type === candidate));
  const visibleFeed = type ? feedRows.filter((row) => row.signal_type === type) : feedRows;

  const stockRows = ok(stocks) ? stocks.data : [];
  const visibleStocks = filterStocksByBias(stockRows, bias);
  const classified = stockRows.filter((row) => row.rsi_bias !== null).length;
  const entry = stockRows.filter((row) => row.rsi_bias === "ENTRY").length;
  const overbought = stockRows.filter((row) => row.rsi_bias === "OVERBOUGHT").length;
  const neutral = stockRows.filter((row) => row.rsi_bias === "NEUTRAL").length;

  const stages = buildStages({
    coverage: ok(coverage) ? coverage.data : null,
    scan: ok(scan) ? scan.data : null,
    members,
    session,
    signals: feedRows,
  });

  // The Trend column is always today's series here — the page is today.
  const sparklines = ok(sparks) ? sparks.data : undefined;

  /** Every chip is also the filter for its own count, and a chip in one section
   *  keeps the other sections' cuts — so a shared link lands on the same view. */
  const hrefWith = (
    next: { state?: WatchlistStateFilter; bias?: StockBiasFilter; type?: SignalType | "" },
    hash: string,
  ): string => {
    const s = next.state ?? state;
    const b = next.bias ?? bias;
    const t = next.type ?? type;
    const query = new URLSearchParams();
    if (s !== "ALL") query.set("state", s.toLowerCase());
    if (b !== "ALL") query.set("bias", b);
    if (t) query.set("type", t);
    const qs = query.toString();
    return `/watchlist${qs ? `?${qs}` : ""}${hash}`;
  };

  return (
    <div className="space-y-4">
      <PageHeading
        title="Watchlist"
        subtitle={
          <>
            The whole day in sequence — the universe the scanner scans, what qualified, what alerted, what it still
            tracks, and what it dropped. Today only; past sessions live on the{" "}
            <Link href="/history" className="link">
              Days
            </Link>{" "}
            page.
          </>
        }
        aside={
          <span className="chip border-line-2 bg-surface-2 text-ink-2">
            {sessionDate ? `${longDate(sessionDate)} · ${orDash(session?.session_status)}` : "no session yet"}
          </span>
        }
      />

      <StageStrip stages={stages} />

      {/* 3 & 5 & 6 ── memberships, what is tracked, what left ───────────────── */}
      <div id="members" className="scroll-mt-20 space-y-4">
        <div className="flex flex-wrap gap-2 text-[12px]">
          <FilterChip
            href={hrefWith({ state: "TRACKED" }, "#members")}
            label="Tracked"
            value={tracked}
            tone="text-positive"
            active={state === "TRACKED"}
          />
          <FilterChip
            href={hrefWith({ state: "WEAKENING" }, "#members")}
            label="Weakening / stale"
            value={weakening}
            tone={weakening > 0 ? "text-amber" : "text-muted"}
            active={state === "WEAKENING"}
          />
          <FilterChip
            href={hrefWith({ state: "REMOVED" }, "#members")}
            label="Removed"
            value={removed}
            tone="text-muted"
            active={state === "REMOVED"}
          />
          <FilterChip
            href={hrefWith({ state: "ALL" }, "#members")}
            label="All memberships"
            value={members.length}
            active={state === "ALL"}
          />
        </div>

        <Panel
          title={sessionDate ? `Members on ${longDate(sessionDate)}` : "Memberships"}
          hint="tracked first, then by score"
        >
          {ok(watch) === false ? (
            <ErrorNote title="Watchlist unavailable" detail={watch.error} />
          ) : visibleMembers.length === 0 && members.length > 0 && state !== "ALL" ? (
            <Empty>
              {STATE_EMPTY[state]} All {members.length} membership(s) of the day are one click away on “All
              memberships”.
            </Empty>
          ) : (
            <WatchlistTable rows={visibleMembers} sparklines={sparklines} />
          )}
        </Panel>

        {ok(watch) && members.length > 0 ? (
          <p className="px-1 text-[11px] leading-snug text-muted">
            Every symbol that qualified today stays in this table even after it is removed — the list is the audit
            trail of what the scanner considered tradeable, not just what is tradeable now. A dash in an indicator
            column means the pipeline had not computed that value yet, not that it measured zero.
          </p>
        ) : null}
      </div>

      {/* 4 ── the day's alerts, with delivery state ─────────────────────────── */}
      <div id="alerts" className="scroll-mt-20 space-y-4">
        <div className="flex flex-wrap gap-2 text-[12px]">
          <FilterChip
            href={hrefWith({ type: "" }, "#alerts")}
            label="All types"
            value={feedRows.length}
            active={type === ""}
          />
          {typesPresent.map((candidate) => (
            <FilterChip
              key={candidate}
              href={hrefWith({ type: candidate }, "#alerts")}
              label={humanise(candidate)}
              value={feedRows.filter((row) => row.signal_type === candidate).length}
              active={type === candidate}
            />
          ))}
        </div>

        <Panel title="Alerts today" hint="newest first — a signal row is written even when delivery fails">
          {ok(feed) === false ? (
            <ErrorNote title="Signal feed unavailable" detail={feed.error} />
          ) : visibleFeed.length === 0 ? (
            <Empty>
              {type
                ? `No ${humanise(type)} alerts today. The counts above are of the full day.`
                : "No alerts yet today. If you expected one, check the health page before assuming the notification was lost — the signal row is written even when Telegram delivery fails."}
            </Empty>
          ) : (
            <SignalsTable signals={visibleFeed} deliveries={ok(deliveries) ? deliveries.data : []} />
          )}
        </Panel>

        {ok(deliveries) === false ? (
          <div className="px-1">
            <ErrorNote title="Delivery states unread" detail={deliveries.error} />
          </div>
        ) : null}
      </div>

      {/* 1 ── the universe the scanner scans ────────────────────────────────── */}
      <div id="universe" className="scroll-mt-20 space-y-4">
        <div className="flex flex-wrap gap-2 text-[12px]">
          <FilterChip href={hrefWith({ bias: "ALL" }, "#universe")} label="In view" value={stockRows.length} active={bias === "ALL"} />
          <FilterChip
            href={hrefWith({ bias: "CLASSIFIED" }, "#universe")}
            label="RSI classified"
            value={classified}
            tone={classified > 0 ? "text-ink-2" : "text-amber"}
            active={bias === "CLASSIFIED"}
          />
          <FilterChip
            href={hrefWith({ bias: "ENTRY" }, "#universe")}
            label="Entry"
            value={entry}
            tone="text-positive"
            active={bias === "ENTRY"}
          />
          <FilterChip
            href={hrefWith({ bias: "OVERBOUGHT" }, "#universe")}
            label="Overbought"
            value={overbought}
            tone="text-negative"
            active={bias === "OVERBOUGHT"}
          />
          <FilterChip
            href={hrefWith({ bias: "NEUTRAL" }, "#universe")}
            label="Neutral"
            value={neutral}
            tone="text-muted"
            active={bias === "NEUTRAL"}
          />
        </div>

        <Panel
          title="F&O universe"
          hint={
            ok(stocks)
              ? `read from the ${source().name} source${bias !== "ALL" ? ` · showing ${visibleStocks.length}` : ""}`
              : undefined
          }
        >
          {ok(stocks) === false ? (
            <ErrorNote title="Universe directory unavailable" detail={stocks.error} />
          ) : stockRows.length > 0 && visibleStocks.length === 0 && bias !== "ALL" ? (
            <Empty>{BIAS_EMPTY[bias]} — the counts above are of the full universe.</Empty>
          ) : (
            <StocksTable rows={visibleStocks} sparklines={ok(dailySparks) ? dailySparks.data : undefined} />
          )}
        </Panel>

        {ok(stocks) && stockRows.length > 0 ? (
          <p className="px-1 text-[11px] leading-snug text-muted">
            Price and change% come from the last two synced daily bars, and the RSI readings from the daily/weekly
            close series (Wilder RSI-14) — the nightly pair. Today&rsquo;s five-minute RSI, the one the entry alert
            fires on, is in the members table above. A dash means that sync has not covered the symbol yet, not that
            the value is zero.
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** The newest `trading_date` present in a mixed set of rows, or null. */
function newestDate(rows: Array<{ trading_date: string | null }>): string | null {
  let newest: string | null = null;
  for (const row of rows) {
    if (row.trading_date && (newest === null || row.trading_date > newest)) newest = row.trading_date;
  }
  return newest;
}
