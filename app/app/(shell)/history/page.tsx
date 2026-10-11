import Link from "next/link";
import { Panel, Metric, ErrorNote, Empty, PageHeading } from "@/components/ui";
import { duration, longDate, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

const LIMITS = [10, 30, 90, 250];

/**
 * Session history — one row per trading day, straight from `v_session_history`,
 * which reads the daily summary the worker writes at finalize plus the session
 * row itself.
 *
 * This is the page that answers "was yesterday's quiet a quiet market or a broken
 * scanner?" A day with zero qualified symbols and a stale time of several minutes
 * is a different answer from a day with zero signals and zero candles at all.
 */
export default async function HistoryPage({ searchParams }: { searchParams: Promise<{ limit?: string }> }) {
  const { limit } = await searchParams;
  const wanted = LIMITS.includes(Number(limit)) ? Number(limit) : 30;
  const days = await panel(() => source().history(wanted));
  const rows = ok(days) ? days.data : [];

  const totalSignals = rows.reduce((sum, row) => sum + row.signals_generated, 0);
  const totalDelivered = rows.reduce((sum, row) => sum + row.telegram_delivered, 0);
  const totalFailed = rows.reduce((sum, row) => sum + row.telegram_failed, 0);
  const totalStale = rows.reduce((sum, row) => sum + row.stale_data_seconds, 0);
  const best = rows.reduce<{ date: string; qualified: number } | null>((acc, row) => {
    if (row.qualified_count === 0) return acc;
    return !acc || row.qualified_count > acc.qualified ? { date: row.trading_date, qualified: row.qualified_count } : acc;
  }, null);

  return (
    <div className="space-y-4">
      <PageHeading
        title="Days"
        subtitle="One row per trading session the worker finalized. A day that never finalized is not here, because nothing was written for it."
        aside={
          <form method="get" className="flex items-center gap-2 text-[12px] text-muted">
            <label htmlFor="limit">Show</label>
            <select id="limit" name="limit" defaultValue={String(wanted)} className="input">
              {LIMITS.map((value) => (
                <option key={value} value={value}>
                  {value} days
                </option>
              ))}
            </select>
            <button type="submit" className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-surface-2">
              Apply
            </button>
          </form>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Sessions shown" value={rows.length} hint={`${rows.filter((row) => row.session_status === "FINALIZED").length} finalized`} />
        <Metric label="Signals raised" value={totalSignals} hint={`${totalDelivered} delivered · ${totalFailed} failed`} tone={totalFailed > 0 ? "negative" : "neutral"} />
        <Metric label="Stale feed time" value={duration(totalStale)} hint="summed across these days" tone={totalStale > 0 ? "amber" : "neutral"} />
        <Metric label="Best day" value={best ? best.qualified : "—"} hint={best ? longDate(best.date) : "no day qualified anything"} />
      </div>

      <Panel title="Session history" hint={`read from the ${source().name} source`}>
        {ok(days) === false ? (
          <ErrorNote title="History unavailable" detail={days.error} />
        ) : rows.length === 0 ? (
          <Empty>
            No finalized sessions. This is expected on a fresh deployment: the summary row is written once at 15:30
            IST, not during the day.
          </Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-line">
                  <th className="th">Date</th>
                  <th className="th">Status</th>
                  <th className="th text-right">Universe</th>
                  <th className="th text-right">Mapped</th>
                  <th className="th text-right">Subscribed</th>
                  <th className="th text-right">Monitored</th>
                  <th className="th text-right">Qualified</th>
                  <th className="th text-right">Signals</th>
                  <th className="th text-right">Delivered</th>
                  <th className="th text-right">Failed</th>
                  <th className="th text-right">Stale</th>
                  <th className="th">Finalized</th>
                  <th className="th">Notes</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.trading_session_id} className="row-hover border-b border-line last:border-0">
                    <td className="td num font-semibold">
                      <Link href={`/watchlist?date=${row.trading_date}`} className="link">
                        {row.trading_date}
                      </Link>
                      <span className="ml-1.5 text-[11px] font-normal text-muted">{longDate(row.trading_date).slice(0, 3)}</span>
                    </td>
                    <td className="td">
                      <span
                        className={`chip ${
                          row.session_status === "FINALIZED"
                            ? "border-line-2 bg-surface-2 text-muted"
                            : row.session_status === "INTERRUPTED"
                              ? "border-negative/40 bg-negative/10 text-negative"
                              : "border-accent/40 bg-accent/10 text-accent"
                        }`}
                      >
                        {orDash(row.session_status)}
                      </span>
                    </td>
                    <td className="td num text-right">{row.universe_count}</td>
                    <td className="td num text-right">{row.mapped_count}</td>
                    <td className="td num text-right">{row.subscribed_count}</td>
                    <td className={`td num text-right ${row.monitored_count < row.subscribed_count ? "text-amber" : ""}`}>
                      {row.monitored_count}
                    </td>
                    <td className={`td num text-right font-semibold ${row.qualified_count > 0 ? "text-positive" : "text-muted"}`}>
                      {row.qualified_count}
                    </td>
                    <td className="td num text-right">{row.signals_generated}</td>
                    <td className={`td num text-right ${row.telegram_delivered > 0 ? "text-positive" : "text-muted"}`}>
                      {row.telegram_delivered}
                    </td>
                    <td className={`td num text-right ${row.telegram_failed > 0 ? "text-negative font-semibold" : "text-muted"}`}>
                      {row.telegram_failed}
                    </td>
                    <td className={`td num text-right ${row.stale_data_seconds > 0 ? "text-amber" : "text-muted"}`}>
                      {duration(row.stale_data_seconds)}
                    </td>
                    <td className="td num text-[12px]">{row.finalized_at ? row.finalized_at.slice(11, 16) : "—"}</td>
                    <td className="td max-w-[260px] text-[12px] text-muted">{orDash(row.notes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {rows.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          <span className="num">Monitored &lt; subscribed</span> means symbols were subscribed but never printed a tick
          in that session — usually an illiquid contract, occasionally a feed problem. It is highlighted because the
          scanner cannot qualify something that never spoke.
        </p>
      ) : null}
    </div>
  );
}
