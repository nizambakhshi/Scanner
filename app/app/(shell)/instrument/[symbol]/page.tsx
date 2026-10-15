import Link from "next/link";
import { notFound } from "next/navigation";
import { Panel, Metric, ErrorNote, Empty, PageHeading } from "@/components/ui";
import { SimChip } from "@/components/SimulatedBanner";
import { RsiBiasBadge, RsiStateBadge, StateBadge, rsiBandTone } from "@/components/badges";
import { CandleChart } from "@/components/CandleChart";
import { EventTimeline } from "@/components/EventTimeline";
import { SectorChip } from "@/components/SectorChip";
import { SignalsTable } from "@/components/SignalsTable";
import { clock, crores, dateTime, longDate, multiple, orDash, pct, price, rsi, score, shares } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { sectorReadsFrom } from "@/lib/sector-reads";
import { sectorFor } from "@/lib/sector-map";
import { source } from "@/lib/source";
import type { RsiBias } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * One instrument, as the scanner saw it.
 *
 * The chart draws only completed candles and the indicator lines from the snapshot
 * rows computed on those same candles, so this is the picture the qualification
 * test actually used — not a live chart with a projection on the end. The event
 * timeline below it explains every state the membership has been in today.
 */
export default async function InstrumentPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  const clean = decodeURIComponent(symbol).slice(0, 32);
  if (!/^[\w.-]+$/.test(clean)) notFound();

  // Same anchor as the watchlist and feed: the active IST trading day. A
  // membership or signal row from a finished session is not shown as if it
  // were current.
  const today = await source().activeTradingDate();

  const [detail, signals, sectorQuotes] = await Promise.all([
    panel(() => source().instrument(clean)),
    panel(() => source().signals({ symbol: clean, tradingDate: today, limit: 50 })),
    panel(() => source().sectorQuotes()),
  ]);

  if (ok(detail) === false) {
    return (
      <div className="space-y-4">
        <PageHeading title={clean.toUpperCase()} />
        <ErrorNote title="Could not read this instrument" detail={detail.error} />
      </div>
    );
  }
  const data = detail.data;
  if (!data) notFound();

  const instrument = data.instrument;
  // A universe row that does not exist is a 404, not a page of dashes: half a
  // symbol page reads as "this one is quiet" rather than "this one is unknown".
  if (!instrument) notFound();
  const { candles, snapshots, events, membership: rawMembership, overview, daily_rsi, weekly_rsi } = data;
  // Only the active day's membership is shown as current — a row from a
  // finished session is older evidence, not today's state.
  const membership = rawMembership && today && rawMembership.trading_date !== today ? null : rawMembership;
  const last = snapshots[snapshots.length - 1] ?? null;
  const firstCandle = candles[0] ?? null;
  const lastCandle = candles[candles.length - 1] ?? null;
  const high = candles.reduce((best, candle) => Math.max(best, candle.high), Number.NEGATIVE_INFINITY);
  const low = candles.reduce((best, candle) => Math.min(best, candle.low), Number.POSITIVE_INFINITY);
  const totalVolume = candles.reduce((sum, candle) => sum + candle.volume, 0);
  const rows = ok(signals) ? signals.data : [];
  // The symbol's NIFTY sector index, and the page's one reading of that index —
  // the chip in the heading and the chips in the feed below share both.
  const sector = sectorFor(instrument.symbol);
  const sectorReads = ok(sectorQuotes) ? sectorReadsFrom(sectorQuotes.data) : undefined;
  // A mapped universe row always has an overview object, but every synced field in
  // it can still be null — that is "the syncs have not covered it", which deserves
  // the note, not a grid of dashes.
  const synced =
    overview !== null &&
    (overview.close !== null ||
      overview.pe_ratio !== null ||
      overview.market_cap_cr !== null ||
      overview.high_52w !== null ||
      overview.daily_rsi !== null ||
      overview.weekly_rsi !== null);

  return (
    <div className="space-y-4">
      <PageHeading
        title={instrument.symbol}
        subtitle={
          <>
            {orDash(instrument.company_name)} · {orDash(instrument.exchange)}
            {instrument.trading_symbol !== instrument.symbol ? ` · trades as ${instrument.trading_symbol}` : ""} ·{" "}
            {instrument.is_nifty500 ? "NIFTY500" : "outside the NIFTY500 filter"} · {orDash(instrument.mapping_status)}
            {instrument.previous_close !== null ? ` · previous close ${price(instrument.previous_close)}` : ""}
          </>
        }
        aside={
          <>
            {membership ? <StateBadge state={membership.current_state} /> : null}
            <SectorChip sector={sector} initial={sector ? (sectorReads?.[sector] ?? null) : null} />
            <SimChip value={candles.some((candle) => candle.is_simulated)} label="SIMULATED SERIES" />
            <Link href="/watchlist" className="link text-[12px]">
              Watchlist
            </Link>
          </>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
        <Metric
          label="Last"
          value={last ? price(last.close) : "—"}
          hint={lastCandle ? `candle ${clock(lastCandle.candle_start_at)}` : "no candle"}
        />
        <Metric
          label="Day change"
          value={last ? pct(last.intraday_change_pct) : "—"}
          hint={instrument.previous_close === null ? "no reference close" : `from ${price(instrument.previous_close)}`}
          tone={last && (last.intraday_change_pct ?? 0) >= 0 ? "positive" : "negative"}
        />
        <Metric
          label="vs VWAP"
          value={last ? pct(last.distance_from_vwap_pct) : "—"}
          hint={last ? `vwap ${price(last.vwap)}` : "no snapshot"}
          tone={last && (last.distance_from_vwap_pct ?? 0) > 0 ? "positive" : "neutral"}
        />
        <Metric
          label="Relative volume"
          value={last ? multiple(last.relative_volume) : "—"}
          hint={last && last.average_volume20 !== null ? `20-candle mean ${shares(last.average_volume20)}` : "needs a full window"}
          tone={last && (last.relative_volume ?? 0) >= 1.5 ? "positive" : "neutral"}
        />
        <Metric
          label="Score"
          value={last ? score(last.score) : "—"}
          hint={last?.rank ? `initial rank ${last.rank}` : "unranked — ranks come from the initial scan only"}
        />
        <Metric
          label="RSI 5m"
          value={last ? rsi(last.rsi_5m) : "—"}
          hint={last ? stateHint(last.rsi_state) : "no snapshot"}
          tone={last?.rsi_state === "ENTRY" ? "positive" : last?.rsi_state === "OVERBOUGHT" ? "negative" : "neutral"}
        />
        <Metric
          label="RSI daily / weekly"
          value={overview ? `${rsi(overview.daily_rsi)} / ${rsi(overview.weekly_rsi)}` : "—"}
          hint={overview ? biasHint(overview.rsi_bias) : "no daily/weekly reading synced"}
          tone={overview?.rsi_bias === "ENTRY" ? "positive" : overview?.rsi_bias === "OVERBOUGHT" ? "negative" : "neutral"}
        />
      </div>

      <Panel
        title={`${instrument.symbol} · 1-minute candles`}
        hint={
          firstCandle && lastCandle
            ? `${candles.length} from ${clock(firstCandle.candle_start_at)} to ${clock(lastCandle.candle_start_at)} · H ${Number.isFinite(high) ? price(high) : "—"} · L ${
                Number.isFinite(low) ? price(low) : "—"
              } · vol ${shares(totalVolume)}`
            : "no candles persisted for this symbol"
        }
      >
        <CandleChart candles={candles} snapshots={snapshots} symbol={instrument.symbol} />
      </Panel>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Qualification" hint="the test as the scanner ran it on the last completed candle">
          {!last ? (
            <Empty>No indicator snapshot has been computed for this symbol.</Empty>
          ) : (
            <div className="space-y-2 px-4 py-3">
              <p className="text-[13px]">
                {last.bullish_qualified ? (
                  <span className="chip border-positive/40 bg-positive/10 text-positive">Qualifying</span>
                ) : last.warming_up ? (
                  <span className="chip border-amber/40 bg-amber/10 text-amber">Warming up</span>
                ) : (
                  <span className="chip border-line-2 bg-surface-2 text-muted">Not qualifying</span>
                )}
                <span className="num ml-2 text-[12px] text-muted">{dateTime(last.candle_start_at)}</span>
                {last.warming_up ? <SimChip value={last.is_simulated} /> : null}
              </p>
              <table className="w-full text-[13px]">
                <tbody>
                  <Indicator label="Close" value={price(last.close)} />
                  <Indicator label="Session VWAP" value={price(last.vwap)} ok={last.close !== null && last.vwap !== null && last.close > last.vwap} />
                  <Indicator label="EMA9" value={price(last.ema9)} />
                  <Indicator label="EMA20" value={price(last.ema20)} ok={last.ema9 !== null && last.ema20 !== null && last.ema9 > last.ema20} />
                  <Indicator label="Relative volume" value={multiple(last.relative_volume)} ok={(last.relative_volume ?? 0) >= 1.5} />
                  <Indicator label="Range today" value={`${price(last.session_low)} – ${price(last.session_high)}`} />
                  <Indicator label="Distance from high" value={pct(last.distance_from_high_pct)} />
                  <Indicator label="Data quality" value={orDash(last.data_quality_status)} />
                  <Indicator label="Config" value={`v${last.config_version}`} />
                </tbody>
              </table>
              {last.reasons.length > 0 || last.failed_reasons.length > 0 ? (
                <div className="flex flex-wrap gap-1.5 pt-1">
                  {last.reasons.map((reason) => (
                    <span key={reason} className="chip border-positive/30 bg-positive/5 text-[10px] text-positive">
                      {reason.replace(/_/g, " ").toLowerCase()}
                    </span>
                  ))}
                  {last.failed_reasons.map((reason) => (
                    <span key={reason} className="chip border-negative/30 bg-negative/5 text-[10px] text-negative">
                      {reason.replace(/_/g, " ").toLowerCase()}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          )}
        </Panel>

        <Panel title="Watchlist history" hint="every state this membership moved through">
          {membership ? (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 border-b border-line px-4 py-3 text-[12px]">
              <span className="text-muted">Entered</span>
              <span className="num text-right">{membership.initial_entry_at ? dateTime(membership.initial_entry_at) : "—"}</span>
              <span className="text-muted">Entry price</span>
              <span className="num text-right">{price(membership.initial_entry_price)}</span>
              <span className="text-muted">Last change</span>
              <span className="num text-right">{membership.latest_state_change_at ? dateTime(membership.latest_state_change_at) : "—"}</span>
              <span className="text-muted">Episodes</span>
              <span className="num text-right">{membership.episode_count}</span>
              <span className="text-muted">Weak streak</span>
              <span className="num text-right">
                {membership.consecutive_weak_candles} consecutive candle(s)
              </span>
              <span className="text-muted">Removal reason</span>
              <span className="text-right">{orDash(membership.removal_reason)}</span>
            </div>
          ) : (
            <p className="px-4 py-3 text-[12px] text-muted">
              {rawMembership
                ? "This symbol's watchlist membership belongs to an earlier session — only the active trading day is shown here."
                : "This symbol has no watchlist membership — it has never qualified, so there is no state to trace."}
            </p>
          )}
          {events.length > 0 ? <EventTimeline events={events.slice(0, 20)} /> : <Empty>No recorded transitions.</Empty>}
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Panel title="Stock details" hint="from the daily-bar and profile syncs">
          {synced ? (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 px-4 py-3 text-[12px]">
              <span className="text-muted">P/E</span>
              <span className="num text-right">{overview.pe_ratio === null ? "—" : overview.pe_ratio.toFixed(2)}</span>
              <span className="text-muted">Market cap</span>
              <span className="num text-right">{crores(overview.market_cap_cr)}</span>
              <span className="text-muted">52-week range</span>
              <span className="num text-right">
                {price(overview.low_52w)} – {price(overview.high_52w)}
              </span>
              <span className="text-muted">All-time high</span>
              <span className="num text-right">{price(overview.ath)}</span>
              <span className="text-muted">All-time low</span>
              <span className="num text-right">{price(overview.atl)}</span>
              <span className="text-muted">Listed</span>
              <span className="num text-right">{orDash(overview.listing_date)}</span>
              <span className="text-muted">Daily bars since</span>
              <span className="num text-right">{orDash(overview.data_start)}</span>
              <span className="text-muted">Last synced bar</span>
              <span className="num text-right">
                {overview.last_bar_date ? `${overview.last_bar_date} · close ${price(overview.close)}` : "—"}
              </span>
              <span className="text-muted">Provider</span>
              <span className="text-right">{orDash(overview.profile_source)}</span>
            </div>
          ) : (
            <p className="px-4 py-3 text-[12px] text-muted">
              No synced daily details for this symbol yet — the daily-bar and profile syncs have not covered it. That is
              a coverage gap, not a stock without a P/E.
            </p>
          )}
        </Panel>

        <Panel
          title="RSI — five-minute, daily, weekly"
          hint="one rule on two horizons: under 40 is the green side, over 60 the red one"
          action={
            <span className="flex items-center gap-1.5">
              <RsiStateBadge state={last?.rsi_state ?? null} reading={last?.rsi_5m ?? null} />
              {overview ? <RsiBiasBadge bias={overview.rsi_bias} /> : null}
            </span>
          }
        >
          {/* The intraday verdict has no history table of its own because it is not a
              synced series: it is the reading on the snapshot the worker just wrote, and
              the per-candle list below is its history. */}
          <p className="border-b border-line px-4 py-2 text-[12px] leading-snug text-muted">
            {last && last.rsi_5m !== null ? (
              <>
                Five-minute RSI measured on <span className="num">{clock(last.candle_start_at)}</span> — Wilder RSI-14
                over today&rsquo;s 5-minute closes, judged against the live trend (close over session VWAP, fast EMA over slow
                EMA). Under 40 while the trend is up is an entry; above 60 while it is down is overbought.
              </>
            ) : (
              "No five-minute reading yet — the series needs fifteen buckets, and a verdict is taken only when one closes."
            )}
          </p>
          <div className="grid gap-4 px-4 py-3 sm:grid-cols-2">
            <RsiReadings
              heading="Daily"
              dateLabel="Bar"
              rows={daily_rsi.map((point) => ({ at: point.trading_date, close: point.close, rsi14: point.rsi14 }))}
            />
            <RsiReadings
              heading="Weekly"
              dateLabel="Week of"
              rows={weekly_rsi.map((point) => ({ at: point.week_start, close: point.close, rsi14: point.rsi14 }))}
            />
          </div>
          <p className="border-t border-line px-4 py-2 text-[11px] leading-snug text-muted">
            The daily and weekly columns are Wilder RSI-14 on adjusted closes: ENTRY needs both timeframes below 40,
            OVERBOUGHT both above 60; anything else is neutral. A dash means fewer bars than the 14-period warmup needs
            — not a reading of zero.
          </p>
        </Panel>
      </div>

      <Panel title="This symbol's signals" hint={`${rows.length} row(s)`}>
        {ok(signals) ? <SignalsTable signals={rows} sectorReads={sectorReads} /> : <ErrorNote title="Signal feed unavailable" detail={signals.error} />}
      </Panel>

      <Panel title="Indicator history" hint="the last 40 completed candles the scanner measured">
        {snapshots.length === 0 ? (
          <Empty>No snapshots.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr className="border-b border-line">
                  <th className="th">Candle</th>
                  <th className="th text-right">Close</th>
                  <th className="th text-right">VWAP</th>
                  <th className="th text-right">EMA9</th>
                  <th className="th text-right">EMA20</th>
                  <th className="th text-right">Rel vol</th>
                  <th className="th text-right">Chg</th>
                  <th className="th text-right">Score</th>
                  <th
                    className="th text-right"
                    title="Wilder RSI-14 on the last five-minute bucket close at or before this candle, and the verdict taken there"
                  >
                    RSI 5m
                  </th>
                  <th className="th">Quality</th>
                </tr>
              </thead>
              <tbody>
                {snapshots
                  .slice(-40)
                  .reverse()
                  .map((snapshot) => (
                    <tr key={snapshot.id ?? snapshot.candle_start_at} className="row-hover border-b border-line last:border-0">
                      <td className="td num">{clock(snapshot.candle_start_at)}</td>
                      <td className="td num text-right">{price(snapshot.close)}</td>
                      <td className="td num text-right">{price(snapshot.vwap)}</td>
                      <td className="td num text-right">{price(snapshot.ema9)}</td>
                      <td className="td num text-right">{price(snapshot.ema20)}</td>
                      <td className="td num text-right">{multiple(snapshot.relative_volume)}</td>
                      <td className={`td num text-right ${(snapshot.intraday_change_pct ?? 0) < 0 ? "text-negative" : "text-positive"}`}>
                        {pct(snapshot.intraday_change_pct)}
                      </td>
                      <td className="td num text-right">{score(snapshot.score)}</td>
                      <td className={`td num text-right ${rsiBandTone(snapshot.rsi_5m)}`}>
                        {rsi(snapshot.rsi_5m)}
                        {snapshot.rsi_state ? (
                          <span className="ml-1 text-[11px] text-muted">{snapshot.rsi_state.toLowerCase()}</span>
                        ) : null}
                      </td>
                      <td className="td">
                        <span className={snapshot.data_quality_status === "OK" ? "text-muted" : "text-amber"}>
                          {orDash(snapshot.data_quality_status)}
                        </span>
                        {snapshot.bullish_qualified ? <span className="ml-1 text-[11px] text-positive">qualified</span> : null}
                        {snapshot.warming_up ? <span className="ml-1 text-[11px] text-amber">warming</span> : null}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <p className="px-1 text-[11px] leading-snug text-muted">
        {longDate(firstCandle?.trading_date ?? null)} · {candles.length} persisted candle(s), {snapshots.length}{" "}
        measured snapshot(s). A symbol with candles and no snapshots means the pipeline never finished evaluating it —
        usually because the session ended or the feed went stale first.
      </p>
    </div>
  );
}

function Indicator({ label, value, ok: passes }: { label: string; value: string; ok?: boolean }) {
  return (
    <tr>
      <td className="py-1 text-muted">{label}</td>
      <td className={`num py-1 text-right ${passes === undefined ? "" : passes ? "text-positive" : "text-negative"}`}>
        {value}
        {passes === undefined ? null : passes ? " ✓" : " ✗"}
      </td>
    </tr>
  );
}

/** One timeframe's readings, newest first — the order a reader compares bands in. */
function RsiReadings({
  heading,
  dateLabel,
  rows,
}: {
  heading: string;
  dateLabel: string;
  rows: { at: string; close: number; rsi14: number }[];
}) {
  return (
    <div>
      <p className="pb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{heading}</p>
      {rows.length === 0 ? (
        <Empty>No readings — the series is shorter than the 14-period warmup.</Empty>
      ) : (
        <table className="w-full border-collapse text-[12px]">
          <thead>
            <tr className="border-b border-line">
              <th className="th">{dateLabel}</th>
              <th className="th text-right">Close</th>
              <th className="th text-right">RSI-14</th>
            </tr>
          </thead>
          <tbody>
            {[...rows].reverse().map((row) => (
              <tr key={row.at} className="border-b border-line last:border-0">
                <td className="td num">{row.at}</td>
                <td className="td num text-right">{price(row.close)}</td>
                <td className={`td num text-right ${rsiBandTone(row.rsi14)}`}>{rsi(row.rsi14)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function biasHint(bias: RsiBias): string {
  if (bias === "ENTRY") return "both timeframes under 40";
  if (bias === "OVERBOUGHT") return "both timeframes above 60";
  if (bias === "NEUTRAL") return "not on the same side of the 40–60 band";
  return "not classified yet";
}

/** The intraday tile's hint: the reading alone is not the verdict, so the sentence
 *  has to carry the trend the rule judged it against. */
function stateHint(state: RsiBias): string {
  if (state === "ENTRY") return "under 40 while the trend is up";
  if (state === "OVERBOUGHT") return "above 60 while the trend is down";
  if (state === "NEUTRAL") return "inside the band, or an extreme the trend denies";
  return "no bucket closed yet";
}
