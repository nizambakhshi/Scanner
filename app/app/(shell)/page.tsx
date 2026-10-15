import Link from "next/link";
import { Panel, Metric, Empty, ErrorNote, PageHeading } from "@/components/ui";
import { SimChip } from "@/components/SimulatedBanner";
import { SectorTile } from "@/components/SectorTile";
import { WatchlistTable } from "@/components/WatchlistTable";
import { SignalsTable } from "@/components/SignalsTable";
import { ago, clock, duration, longDate, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { renderNow } from "@/lib/render-time";
import { sectorReadsFrom } from "@/lib/sector-reads";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * The board: the four questions an operator asks when the market opens.
 *
 * 1. Is the feed alive and is the worker keeping up?
 * 2. What qualifies right now?
 * 3. What has been alerted, and did it actually arrive?
 * 4. Is anything about the data itself untrustworthy?
 *
 * Each of those is an independent read. A database outage must break the panels it
 * touches and leave the rest standing — a screen that goes blank everywhere is
 * indistinguishable from a screen that has nothing to show.
 */
export default async function BoardPage() {
  // The board is anchored on the IST calendar date, same as the watchlist: the
  // memberships and the feed are read for today, never for "whatever the
  // newest rows happen to be".
  const today = await source().activeTradingDate();

  const [worker, coverage, watchlist, alerts, initialScan, days, sectors, sparks] = await Promise.all([
    panel(async () => source().workerStatus()),
    panel(async () => source().coverage()),
    panel(async () => source().currentWatchlist(today)),
    panel(async () => source().signals({ tradingDate: today, limit: 12 })),
    panel(async () => source().initialScan()),
    panel(async () => source().history(2)),
    panel(async () => source().sectorQuotes()),
    panel(async () => source().intradaySparklines()),
  ]);

  const status = ok(worker) ? worker.data[0] ?? null : null;
  const counts = ok(coverage) ? coverage.data : null;
  // Belt to the source filter's braces — today's rows only, in code too.
  const rows = (ok(watchlist) ? watchlist.data : []).filter((row) => !today || row.trading_date === today);
  const recent = ok(alerts) ? alerts.data : [];
  const scan = ok(initialScan) ? initialScan.data : null;
  const session = ok(days) && today ? (days.data.find((row) => row.trading_date === today) ?? null) : null;
  // A failed series read drops the Trend column entirely — a column of dashes
  // would suggest the data was read and found empty.
  const sparklines = ok(sparks) ? sparks.data : undefined;
  // One fold, two consumers: the tile takes the raw rows, the chips take this.
  const sectorReads = ok(sectors) ? sectorReadsFrom(sectors.data) : undefined;

  const active = rows.filter((row) => row.current_state === "ACTIVE" || row.current_state === "REQUALIFIED");
  const stale = rows.filter((row) => row.current_state === "DATA_STALE");
  const warming = rows.filter((row) => row.warming_up);
  const failedDelivery = recent.filter((row) => row.telegram_failed);
  const now = renderNow();

  return (
    <div className="space-y-4">
      <PageHeading
        title="Board"
        subtitle={
          <>
            {today
              ? `${longDate(today)} · ${session ? orDash(session.session_status) : "no session yet"}`
              : "No trading day could be named."}{" "}
            The scanner acts only on completed 1-minute candles, so every number here is at most one candle behind the tape.
          </>
        }
        aside={
          status ? (
            <FeedChip feed={status.feed_status} lastTickAt={status.last_market_data_at} now={now} />
          ) : null
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Metric label="On watchlist" value={active.length} hint={`${rows.length} membership(s) today`} tone={active.length > 0 ? "positive" : "neutral"} />
        <Metric
          label="Universe"
          value={counts ? counts.mapped_count : "—"}
          hint={counts ? `${counts.fno_count} F&O underlyings scanned · of ${counts.nifty500_count} mapped` : "coverage unread"}
        />
        <Metric
          label="Monitored"
          value={status ? status.monitored_instrument_count : "—"}
          hint={status ? `subscribed ${status.subscribed_instrument_count}` : "heartbeat unread"}
        />
        <Metric
          label="Alerts today"
          value={session ? session.signals_generated : recent.length}
          hint={
            session
              ? `${session.telegram_delivered} delivered · ${session.telegram_failed} failed`
              : "no daily summary yet"
          }
          tone={session && session.telegram_failed > 0 ? "negative" : "neutral"}
        />
        <Metric
          label="Queue"
          value={status ? status.pending_notification_count : "—"}
          hint={status ? `depth ${status.queue_depth}` : "heartbeat unread"}
          tone={status && status.pending_notification_count > 20 ? "amber" : "neutral"}
        />
        <Metric
          label="Stale today"
          value={session ? duration(session.stale_data_seconds) : "—"}
          hint={stale.length > 0 ? `${stale.length} membership(s) frozen now` : "no membership frozen by staleness"}
          tone={session && session.stale_data_seconds > 0 ? "amber" : "neutral"}
        />
      </div>

      <Panel
        title="Nifty sectors"
        hint="index quotes off the same feed, for context — never a signal"
      >
        {ok(sectors) ? (
          <SectorTile initial={sectors.data} />
        ) : (
          <ErrorNote title="Sector quotes unavailable" detail={sectors.error} />
        )}
      </Panel>

      <Panel
        title="Daily watchlist"
        hint="qualifiers first, then by score"
        action={
          <Link href="/watchlist" className="link text-[12px]">
            Full table →
          </Link>
        }
      >
        {ok(watchlist) ? (
          <WatchlistTable rows={rows.slice(0, 8)} sparklines={sparklines} sectorReads={sectorReads} />
        ) : (
          <ErrorNote title="Watchlist unavailable" detail={watchlist.error} />
        )}
        {warming.length > 0 ? (
          <p className="border-t border-line px-4 py-2 text-[11px] text-muted">
            {warming.length} membership(s) still warming up — EMA20 needs a full window before the scanner will judge
            them.
          </p>
        ) : null}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Worker" hint={status ? `heartbeat ${ago(status.updated_at, now)}` : undefined}>
          {ok(worker) === false ? (
            <ErrorNote title="Heartbeat unavailable" detail={worker.error} />
          ) : !status ? (
            <Empty>No worker has written a heartbeat. Nothing is being monitored.</Empty>
          ) : (
            <dl className="divide-y divide-line text-[13px]">
              <Row label="Worker">{status.worker_id}</Row>
              <Row label="Status">
                <span className="num">{orDash(status.status)}</span>
                <SimChip value={status.simulation_label} label="SIM MODE" />
              </Row>
              <Row label="Feed">
                <span className="num">{orDash(status.feed_status)}</span>
              </Row>
              <Row label="Last tick">
                <span className="num">{status.last_market_data_at ? clock(status.last_market_data_at) : "never"}</span>
                <span className="ml-1 text-muted">({ago(status.last_market_data_at, now)})</span>
              </Row>
              <Row label="Last candle">
                <span className="num">{status.last_completed_candle_at ? clock(status.last_completed_candle_at) : "never"}</span>
              </Row>
              <Row label="Scan mode">{orDash(status.scan_mode)}</Row>
              <Row label="Latency">
                <span className="num">{status.processing_latency_ms === null ? "—" : `${status.processing_latency_ms.toFixed(0)} ms`}</span>
              </Row>
              {status.last_error ? (
                <div className="px-4 py-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-negative">Last error</p>
                  <p className="num mt-1 break-words text-[12px] text-ink-2">{status.last_error}</p>
                </div>
              ) : null}
            </dl>
          )}
        </Panel>

        <Panel title="Instrument coverage" hint="mapping, not market data">
          {ok(coverage) === false ? (
            <ErrorNote title="Coverage unavailable" detail={coverage.error} />
          ) : !counts ? (
            <Empty>The instruments table is empty — the universe has not been downloaded.</Empty>
          ) : (
            <dl className="divide-y divide-line text-[13px]">
              <Row label="NIFTY500 universe">
                <span className="num">{counts.nifty500_count}</span>
              </Row>
              <Row label="Mapped">
                <span className="num text-positive">{counts.mapped_count}</span>
              </Row>
              <Row label="F&O flagged">
                <span className="num">{counts.fno_count}</span>
              </Row>
              <Row label="Unmapped">
                <span className={`num ${counts.unmapped_count > 0 ? "text-amber" : ""}`}>{counts.unmapped_count}</span>
              </Row>
              <Row label="Ambiguous">
                <span className={`num ${counts.ambiguous_count > 0 ? "text-amber" : ""}`}>{counts.ambiguous_count}</span>
              </Row>
              <Row label="Invalid">
                <span className={`num ${counts.invalid_count > 0 ? "text-negative" : ""}`}>{counts.invalid_count}</span>
              </Row>
              <Row label="Active">
                <span className="num">{counts.active_universe_count}</span>
              </Row>
            </dl>
          )}
        </Panel>

        <Panel title="Initial scan" hint="the ranked set the session opened with">
          {ok(initialScan) === false ? (
            <ErrorNote title="Initial scan journal unread" detail={initialScan.error} />
          ) : !scan ? (
            <Empty>No initial scan has been journaled for this deployment.</Empty>
          ) : (
            <div className="space-y-2">
              {scan.skipped ? <p className="text-[12px] text-amber">Skipped: {scan.skipped}</p> : null}
              <p className="text-[12px] text-muted">
                {scan.completed_at ? `Completed ${clock(scan.completed_at)} IST` : "Not completed"} · universe{" "}
                {orDash(scan.universe)} · {orDash(scan.candles_required)} candles required
              </p>
              {scan.counts ? (
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(scan.counts).map(([key, value]) => (
                    <span key={key} className="chip border-line-2 bg-surface-2 text-ink-2 normal-case">
                      {key.replace(/_/g, " ")} <span className="num font-bold">{value}</span>
                    </span>
                  ))}
                </div>
              ) : null}
              {scan.ranked && scan.ranked.length > 0 ? (
                <ul className="flex flex-wrap gap-2 pt-1">
                  {scan.ranked.map((symbol, index) => (
                    <li key={symbol}>
                      <Link href={`/instrument/${encodeURIComponent(symbol)}`} className="num link text-[13px]">
                        {index + 1}. {symbol}
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-[12px] text-muted">Nothing qualified at the initial scan.</p>
              )}
            </div>
          )}
        </Panel>
      </div>

      <Panel
        title="Recent alerts"
        hint="signals with their delivery state"
        action={
          <Link href="/watchlist#alerts" className="link text-[12px]">
            Today&rsquo;s feed →
          </Link>
        }
      >
        {ok(alerts) ? (
          <SignalsTable signals={recent} sectorReads={sectorReads} compact />
        ) : (
          <ErrorNote title="Alert feed unavailable" detail={alerts.error} />
        )}
      </Panel>

      {failedDelivery.length > 0 ? (
        <div className="border-l-2 border-negative bg-negative/5 px-4 py-3">
          <p className="text-[12px] font-semibold uppercase tracking-wide text-negative">
            {failedDelivery.length} alert(s) in this window did not reach Telegram
          </p>
          <p className="mt-1 text-[12px] leading-snug text-ink-2">
            A signal row is written even when delivery fails, so the dashboard can show setups you were never told
            about. Check the queue depth on the health page.
          </p>
        </div>
      ) : null}

      {active.length === 0 && rows.length === 0 && ok(watchlist) ? (
        <div className="border-l-2 border-line-2 bg-surface-2 px-4 py-3">
          <p className="text-[13px] font-semibold">Nothing qualifies.</p>
          <p className="mt-1 text-[12px] leading-snug text-muted">
            That is a normal state, not an error: the conditions are close above session VWAP, EMA9 above EMA20, and
            relative volume at or above the configured multiple. An empty watchlist with a streaming feed means the
            market is not offering setups that match the config — not that the scanner is broken.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-2">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="flex items-baseline gap-1.5 text-right text-[13px]">{children}</dd>
    </div>
  );
}

function FeedChip({ feed, lastTickAt, now }: { feed: string; lastTickAt: string | null; now: number }) {
  const streaming = feed === "STREAMING";
  const minutes = lastTickAt ? (now - Date.parse(lastTickAt)) / 60_000 : null;
  const behind = minutes !== null && minutes > 2;
  const tone = !streaming || behind ? "border-negative/40 bg-negative/10 text-negative" : "border-positive/40 bg-positive/10 text-positive";
  return (
    <span className={`chip ${tone}`} title={`feed_status from the worker heartbeat, ${orDash(lastTickAt)}`}>
      {streaming && !behind ? "Feed streaming" : streaming ? `Feed silent ${minutes ? `${Math.round(minutes)} min` : ""}` : orDash(feed)}
    </span>
  );
}
