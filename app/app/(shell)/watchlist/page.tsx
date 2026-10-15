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
import { sectorReadsFrom } from "@/lib/sector-reads";
import { source } from "@/lib/source";
import {
  compareMembers,
  compareStocks,
  memberSortDir,
  parseMemberSort,
  parseSortDir,
  parseStockSort,
  stockSortDir,
  toggledDir,
  type MemberSortKey,
  type SortDir,
  type StockSortKey,
} from "@/lib/sorting";
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
 * "Today" is the IST calendar date right now, not the newest row in the
 * database: the watchlist and feed are read for that exact date, so a finished
 * session can never masquerade as today's. Before the worker has written
 * anything for today, the page shows an empty day with today's date — never
 * yesterday's list. Yesterday lives on the Days page.
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

  // Sorting, one axis per table (members: sort/dir, universe: usort/udir), so
  // sorting the universe never disturbs the members' order and vice versa. An
  // unknown key sorts nothing — the source's own meaningful order stands.
  const mSort = parseMemberSort(params.sort);
  const uSort = parseStockSort(params.usort);
  const mDir = parseSortDir(params.dir, mSort ? memberSortDir(mSort) : "desc");
  const uDir = parseSortDir(params.udir, uSort ? stockSortDir(uSort) : "desc");

  // The anchor: the IST calendar date — a property of the clock, never of how
  // far the worker has written. The postgres source computes it; the replay
  // source answers with its fixture's scripted session date.
  const today = await source().activeTradingDate();

  // Wave one: the day's session row and every stage source, one read per panel
  // so a failed read breaks its own panel and nothing else. history(3) may be
  // all from previous sessions when today's row does not exist yet — the row
  // for *today* is picked out below, and a missing one is a fact, not an error.
  const [days, coverage, scan, stocks, sparks, dailySparks, sectorQuotes] = await Promise.all([
    panel(() => source().history(3)),
    panel(() => source().coverage()),
    panel(() => source().initialScan()),
    panel(() => source().stocksOverview()),
    panel(() => source().intradaySparklines()),
    panel(() => source().dailySparklines()),
    panel(() => source().sectorQuotes()),
  ]);
  const todaySession = ok(days) && today ? (days.data.find((row) => row.trading_date === today) ?? null) : null;

  // First paint for every sector chip on the page; the chips then keep
  // themselves current off the shared client poller.
  const sectorReads = ok(sectorQuotes) ? sectorReadsFrom(sectorQuotes.data) : undefined;

  // Wave two: today's memberships and feed, filtered at the data source when the
  // session date is known.
  const [watch, feed] = await Promise.all([
    panel(() => source().currentWatchlist(today)),
    panel(() => source().signals({ tradingDate: today, limit: FEED_LIMIT })),
  ]);
  const allMembers = ok(watch) ? watch.data : [];
  const allFeed = ok(feed) ? feed.data : [];

  // The belt to the source filter's braces: cut in code too, so a view that
  // answered too broadly (or a fixture with mixed dates) cannot leak a
  // previous session into today's page.
  const members = today ? allMembers.filter((row) => row.trading_date === today) : allMembers;
  const feedRows = today ? allFeed.filter((row) => row.trading_date === today) : allFeed;

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

  // The comparator runs on copies — a view's array is never rearranged in place.
  const membersShown = mSort ? [...visibleMembers].sort(compareMembers(mDir, mSort)) : visibleMembers;
  const stocksShown = uSort ? [...visibleStocks].sort(compareStocks(uDir, uSort)) : visibleStocks;

  const stages = buildStages({
    coverage: ok(coverage) ? coverage.data : null,
    scan: ok(scan) ? scan.data : null,
    members,
    session: todaySession,
    signals: feedRows,
  });

  // The Trend column is always today's series here — the page is today.
  const sparklines = ok(sparks) ? sparks.data : undefined;

  /** Every chip is also the filter for its own count, and a chip in one section
   *  keeps the other sections' cuts — so a shared link lands on the same view.
   *  The sort state rides along the same way: changing a filter never resets
   *  how a table is ordered. */
  const hrefWith = (
    next: {
      state?: WatchlistStateFilter;
      bias?: StockBiasFilter;
      type?: SignalType | "";
      sort?: MemberSortKey;
      dir?: SortDir;
      usort?: StockSortKey;
      udir?: SortDir;
    },
    hash: string,
  ): string => {
    const s = next.state ?? state;
    const b = next.bias ?? bias;
    const t = next.type ?? type;
    const ms = next.sort ?? mSort;
    const us = next.usort ?? uSort;
    const md = next.dir ?? (ms ? mDir : null);
    const ud = next.udir ?? (us ? uDir : null);
    const query = new URLSearchParams();
    if (s !== "ALL") query.set("state", s.toLowerCase());
    if (b !== "ALL") query.set("bias", b);
    if (t) query.set("type", t);
    if (ms) {
      query.set("sort", ms);
      if (md) query.set("dir", md);
    }
    if (us) {
      query.set("usort", us);
      if (ud) query.set("udir", ud);
    }
    const qs = query.toString();
    return `/watchlist${qs ? `?${qs}` : ""}${hash}`;
  };

  /** A members column's header link: the already-active column flips, a new
   *  column takes its own default direction, and every other parameter — both
   *  filters and the universe's sort — is preserved. */
  const memberSortHref = (column: string): string => {
    const key = column as MemberSortKey;
    return hrefWith({ sort: key, dir: toggledDir(mDir, key === mSort, memberSortDir(key)) }, "#members");
  };

  const universeSortHref = (column: string): string => {
    const key = column as StockSortKey;
    return hrefWith({ usort: key, udir: toggledDir(uDir, key === uSort, stockSortDir(key)) }, "#universe");
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
            {today
              ? `${longDate(today)} · ${todaySession ? orDash(todaySession.session_status) : "no session yet"}`
              : "no session yet"}
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
          title={today ? `Members on ${longDate(today)}` : "Memberships"}
          hint={mSort ? "sorted — click the sorted column again to flip" : "tracked first, then by score — click a header to sort"}
        >
          {ok(watch) === false ? (
            <ErrorNote title="Watchlist unavailable" detail={watch.error} />
          ) : members.length === 0 ? (
            <Empty>
              {today
                ? `Nothing has qualified for ${longDate(today)} yet. Memberships appear when a completed bullish candle clears every gate; a previous session's list is never shown here.`
                : "No memberships read — the data source could not name an active trading day."}
            </Empty>
          ) : visibleMembers.length === 0 && state !== "ALL" ? (
            <Empty>
              {STATE_EMPTY[state]} All {members.length} membership(s) of the day are one click away on “All
              memberships”.
            </Empty>
          ) : (
            <WatchlistTable
              rows={membersShown}
              sparklines={sparklines}
              sectorReads={sectorReads}
              sort={{ key: mSort, dir: mDir, hrefFor: memberSortHref }}
            />
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
            <SignalsTable signals={visibleFeed} deliveries={ok(deliveries) ? deliveries.data : []} sectorReads={sectorReads} />
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
            <StocksTable
              rows={stocksShown}
              sparklines={ok(dailySparks) ? dailySparks.data : undefined}
              sectorReads={sectorReads}
              sort={{ key: uSort, dir: uDir, hrefFor: universeSortHref }}
            />
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
