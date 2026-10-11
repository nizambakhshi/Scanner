import { Panel, Metric, ErrorNote, Empty, PageHeading } from "@/components/ui";
import { SimChip } from "@/components/SimulatedBanner";
import { ago, dateTime, duration, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { renderNow } from "@/lib/render-time";
import { authSetupProblem } from "@/lib/auth/users";
import { describeSource } from "@/lib/source";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * Where every number on this dashboard comes from.
 *
 * This page exists because the honest failure mode of a tool like this is not a
 * crash, it is a plausible-looking screen built on data that is not what it claims
 * to be. So the source is stated first, a database failure is shown as a failure
 * (never as a silent switch to the sample), and the only secrets reported here are
 * variable *names* that are missing — a value can never reach this render path.
 */
export default async function HealthPage() {
  const [status, workers, coverage, config] = await Promise.all([
    panel(describeSource),
    panel(() => source().workerStatus()),
    panel(() => source().coverage()),
    panel(() => source().config()),
  ]);

  const info = ok(status) ? status.data : null;
  const beats = ok(workers) ? workers.data : [];
  const now = renderNow();
  const authProblem = authSetupProblem();

  return (
    <div className="space-y-4">
      <PageHeading
        title="Health"
        subtitle="The data source, the worker's own heartbeat, and what would have to be true for this screen to be lying to you."
      />

      {info ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric
            label="Data source"
            value={info.name === "postgres" ? "PostgreSQL" : info.name === "replay" ? "Replay sample" : "None"}
            tone={info.name === "postgres" ? "positive" : info.name === "replay" ? "amber" : "negative"}
            hint={info.reason}
          />
          <Metric
            label="Database"
            value={info.database === null ? "not used" : info.database.ok ? "connected" : "failed"}
            tone={info.database === null ? "neutral" : info.database.ok ? "positive" : "negative"}
            hint={info.database?.detail ?? "no connection attempted in replay mode"}
          />
          <Metric
            label="TLS"
            value={info.databaseSsl}
            tone={info.databaseSsl === "disable" ? "negative" : "neutral"}
            hint={
              info.name === "postgres"
                ? info.preparedStatements
                  ? "prepared statements on"
                  : "transaction pooler detected — statements inlined"
                : "no connection is made in replay mode"
            }
          />
          <Metric
            label="Sample readable"
            value={info.name === "replay" ? (info.sample.readable ? "yes" : "no") : "n/a"}
            tone={info.name === "replay" && !info.sample.readable ? "negative" : "neutral"}
            hint={info.name === "replay" ? (info.sample.detail ?? "data/replay-fixture.json") : "postgres mode never reads the sample"}
          />
        </div>
      ) : (
        <ErrorNote
          title="Source status unread"
          detail={ok(status) ? "The source described no status at all." : status.error}
        />
      )}

      <Panel title="Configuration gaps" hint="names only, never values">
        {info && info.missing.length > 0 ? (
          <ul className="divide-y divide-line text-[13px]">
            {info.missing.map((name) => (
              <li key={name} className="flex items-baseline justify-between gap-3 px-4 py-2">
                <span className="num text-ink-2">{name}</span>
                <span className="text-[12px] text-amber">not set</span>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>Nothing required is missing for the mode this process is running in.</Empty>
        )}
        {authProblem ? (
          <div className="border-t border-line px-4 py-3">
            <ErrorNote title="Sign-in problem" detail={authProblem} />
          </div>
        ) : null}
        {info && info.name === "postgres" && info.database && !info.database.ok ? (
          <div className="border-t border-line px-4 py-3">
            <p className="text-[12px] leading-snug text-ink-2">
              The database is configured but unreachable, so every panel above this note is reporting a failed read.
              This app does not fall back to the replay sample when that happens — a screen of synthetic candles that
              looks like a market is the failure this design refuses.
            </p>
          </div>
        ) : null}
      </Panel>

      <Panel title="Worker heartbeats" hint="what the ingestion process says about itself">
        {ok(workers) === false ? (
          <ErrorNote title="Heartbeats unread" detail={workers.error} />
        ) : beats.length === 0 ? (
          <Empty>No heartbeat has ever been written. Nothing is being scanned.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-line">
                  <th className="th">Worker</th>
                  <th className="th">Status</th>
                  <th className="th">Feed</th>
                  <th className="th text-right">Subscribed</th>
                  <th className="th text-right">Monitored</th>
                  <th className="th text-right">Watchlist</th>
                  <th className="th text-right">Pending</th>
                  <th className="th text-right">Latency</th>
                  <th className="th">Last tick</th>
                  <th className="th">Updated</th>
                  <th className="th">Last error</th>
                </tr>
              </thead>
              <tbody>
                {beats.map((beat) => (
                  <tr key={beat.worker_id} className="row-hover border-b border-line last:border-0">
                    <td className="td">
                      <span className="num font-semibold">{beat.worker_id}</span>
                      <div className="mt-1 flex items-center gap-1">
                        <SimChip value={beat.simulation_label} label="SIM" />
                        <span className="text-[11px] text-muted">{orDash(beat.scan_mode)}</span>
                      </div>
                    </td>
                    <td className="td num">{orDash(beat.status)}</td>
                    <td className="td">
                      <span
                        className={`chip ${
                          beat.feed_status === "STREAMING"
                            ? "border-positive/40 bg-positive/10 text-positive"
                            : beat.feed_status === "STALE"
                              ? "border-amber/40 bg-amber/10 text-amber"
                              : "border-negative/40 bg-negative/10 text-negative"
                        }`}
                      >
                        {orDash(beat.feed_status)}
                      </span>
                    </td>
                    <td className="td num text-right">{beat.subscribed_instrument_count}</td>
                    <td className={`td num text-right ${beat.monitored_instrument_count < beat.subscribed_instrument_count ? "text-amber" : ""}`}>
                      {beat.monitored_instrument_count}
                    </td>
                    <td className="td num text-right">{beat.active_watchlist_count}</td>
                    <td className={`td num text-right ${beat.pending_notification_count > 0 ? "text-amber" : ""}`}>
                      {beat.pending_notification_count}
                    </td>
                    <td className="td num text-right">
                      {beat.processing_latency_ms === null ? "—" : `${Math.round(beat.processing_latency_ms)} ms`}
                    </td>
                    <td className="td num text-[12px]">
                      {beat.last_market_data_at ? ago(beat.last_market_data_at, now) : "never"}
                      <div className="text-[11px] text-muted">{beat.last_market_data_at ? dateTime(beat.last_market_data_at) : ""}</div>
                    </td>
                    <td className="td num text-[12px]">{beat.updated_at ? ago(beat.updated_at, now) : "—"}</td>
                    <td className="td max-w-[260px] break-words text-[12px] text-negative">{orDash(beat.last_error)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Universe" hint="mapping state, from v_instrument_coverage">
          {ok(coverage) === false ? (
            <ErrorNote title="Coverage unread" detail={coverage.error} />
          ) : !coverage.data ? (
            <Empty>No instruments.</Empty>
          ) : (
            <ul className="divide-y divide-line text-[13px]">
              {[
                ["NIFTY500 universe", coverage.data.nifty500_count, ""],
                ["Mapped", coverage.data.mapped_count, "text-positive"],
                ["Unmapped", coverage.data.unmapped_count, coverage.data.unmapped_count > 0 ? "text-amber" : ""],
                ["Ambiguous", coverage.data.ambiguous_count, coverage.data.ambiguous_count > 0 ? "text-amber" : ""],
                ["Invalid", coverage.data.invalid_count, coverage.data.invalid_count > 0 ? "text-negative" : ""],
                ["Active", coverage.data.active_universe_count, ""],
              ].map(([label, value, tone]) => (
                <li key={String(label)} className="flex items-baseline justify-between px-4 py-2">
                  <span className="text-[12px] text-muted">{label}</span>
                  <span className={`num font-semibold ${tone}`}>{value}</span>
                </li>
              ))}
              <li className="px-4 py-2 text-[11px] text-muted">
                Last verified: {coverage.data.last_verified_at ? dateTime(coverage.data.last_verified_at) : "never recorded"}
              </li>
            </ul>
          )}
        </Panel>

        <Panel title="Active config" hint="the thresholds the scanner is judging against">
          {ok(config) === false ? (
            <ErrorNote title="Config unread" detail={config.error} />
          ) : !config.data ? (
            <Empty>No scanner config row.</Empty>
          ) : (
            <ul className="divide-y divide-line text-[13px]">
              <Line label="Name">{config.data.name}</Line>
              <Line label="Version">v{config.data.config_version}</Line>
              <Line label="Enabled">{config.data.is_enabled ? "yes" : "no"}</Line>
              <Line label="Universe">{config.data.universe}</Line>
              <Line label="EMA">{config.data.fast_ema_period} / {config.data.slow_ema_period}</Line>
              <Line label="Rel vol ≥">{config.data.relative_volume_threshold}×</Line>
              <Line label="Cooldown">{duration(config.data.alert_cooldown_minutes * 60)}</Line>
              <Line label="Removal after">{config.data.removal_confirm_candles} weak candles</Line>
              <Line label="Stale after">{duration(config.data.feed_staleness_threshold_seconds)}</Line>
              <Line label="Candle grace">{config.data.candle_grace_seconds}s</Line>
              <Line label="Initial scan">{config.data.initial_scan_candles} candles</Line>
              <Line label="Max subscribed">{config.data.max_subscribed_instruments}</Line>
            </ul>
          )}
        </Panel>
      </div>

      <Panel title="What this screen cannot tell you" hint="stated plainly">
        <ul className="grid gap-2 px-4 py-3 text-[12px] leading-snug text-ink-2 md:grid-cols-2">
          <li className="border-l-2 border-line-2 pl-3">
            It cannot tell you whether a setup is worth trading. The scanner is a filter over price and volume, not a
            prediction; nothing here is advice and nothing here knows about news, corporate actions or circuit limits.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            It cannot tell you the broker session is alive. Authentication lives in the worker; a dead login shows up
            here only as a feed that stops, which is why the heartbeat age and the last tick are printed together.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            It cannot see orders or positions. This app has no trading capability and no credentials that could give it
            one — the dashboard reads a database the worker writes.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            It cannot prove Telegram delivered a message. Delivery status is the record the worker keeps of the provider
            response; a provider that accepts and never sends is invisible from here.
          </li>
        </ul>
      </Panel>
    </div>
  );
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <li className="flex items-baseline justify-between gap-3 px-4 py-2">
      <span className="text-[12px] text-muted">{label}</span>
      <span className="num text-[13px]">{children}</span>
    </li>
  );
}
