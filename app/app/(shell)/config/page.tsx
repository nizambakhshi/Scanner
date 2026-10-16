import Link from "next/link";
import { ConfigEditor } from "@/components/ConfigEditor";
import { Panel, ErrorNote, Empty, PageHeading } from "@/components/ui";
import { duration, multiple } from "@/lib/format";
import { requireAdminPage } from "@/lib/auth/session";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * The active scanner configuration, with an operator edit path.
 *
 * Each value is shown next to the behaviour it causes, because a bare
 * `relative_volume_threshold: 1.5` tells an operator nothing about why a symbol is
 * missing from the watchlist. "Edit values" opens a form for the qualification
 * and watchlist numbers; saving inserts a NEW versioned `scanner_configs` row
 * (history is never overwritten) and journals the change. The worker adopts
 * the newest row when it opens its next session — a save changes what the next
 * session qualifies on, not what the running session is using, and the form
 * says so.
 */
export default async function ConfigPage() {
  await requireAdminPage();
  const [config, scan] = await Promise.all([panel(() => source().config()), panel(() => source().initialScan())]);
  const row = ok(config) ? config.data : null;
  const scanSummary = ok(scan) ? scan.data : null;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Configuration"
        subtitle="The thresholds the scanner judged today's candles against. Edit values to change what the next session qualifies on — each save is a new versioned row, and the running session keeps what it started with."
        aside={row ? <span className="chip border-line-2 bg-surface-2 text-ink-2">config v{row.config_version} · {row.name}</span> : null}
      />

      {ok(config) === false ? (
        <ErrorNote title="Configuration unread" detail={config.error} />
      ) : !row ? (
        <Panel title="Configuration">
          <Empty>
            No enabled <span className="num">scanner_configs</span> row exists. The worker falls back to its built-in
            defaults, which are not what this page would claim.
          </Empty>
        </Panel>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Panel title="Qualification" hint="§10 — a candle must pass all three">
            <dl className="divide-y divide-line text-[13px]">
              <Spec
                label="Close above session VWAP"
                value="required"
                note="Session VWAP is rebuilt from the day's cumulative traded value and quantity; a subscribed contract that never printed a tick contributes nothing to it."
              />
              <Spec
                label="Fast EMA above slow EMA"
                value={`${row.fast_ema_period} > ${row.slow_ema_period}`}
                note={`Seeded from ${row.slow_ema_period} candles of closes. Before that a symbol is WARMING_UP and cannot qualify, because an EMA built from four prints is not a trend.`}
              />
              <Spec
                label="Relative volume"
                value={multiple(row.relative_volume_threshold)}
                note={`Last completed candle against the mean of the previous ${row.volume_lookback}. Computed only for symbols with a full window.`}
              />
            </dl>
            <ConfigEditor row={row} />
          </Panel>

          <Panel title="Watchlist behaviour" hint="§11 — how long something stays">
            <dl className="divide-y divide-line text-[13px]">
              <Spec
                label="Removal confirmation"
                value={`${row.removal_confirm_candles} weak candles`}
                note="A single weak candle marks WEAKENING. Only the full streak removes, so one noisy minute cannot delete a membership."
              />
              <Spec
                label="Alert cooldown"
                value={duration(row.alert_cooldown_minutes * 60)}
                note="Measured against alerts a person actually received. A signal whose delivery was suppressed does not restart the clock."
              />
              <Spec
                label="Rank weights"
                value={Object.entries(row.rank_weights)
                  .map(([key, value]) => `${key.replace(/_/g, " ")} ${value}`)
                  .join(" · ")}
                note={`Applied once, at the initial scan after ${row.initial_scan_candles} candles. Symbols that qualify later join unranked.`}
              />
            </dl>
          </Panel>

          <Panel title="Data integrity" hint="§7, §12 — when the scanner stops trusting the feed">
            <dl className="divide-y divide-line text-[13px]">
              <Spec
                label="Feed staleness"
                value={duration(row.feed_staleness_threshold_seconds)}
                note="Beyond it, memberships freeze: no state change, no removal. An instrument can only be judged on evidence."
              />
              <Spec
                label="Candle grace"
                value={`${row.candle_grace_seconds}s`}
                note="Late prints still land in the minute they belong to. The engine never discards data for arriving a second after the boundary."
              />
              <Spec
                label="Subscription cap"
                value={String(row.max_subscribed_instruments)}
                note={`Universe ${row.universe}. Beyond the cap the worker subscribes what it can and says so in the coverage numbers.`}
              />
            </dl>
          </Panel>

          <Panel title="As used today" hint="what the last initial scan actually did">
            {scanSummary ? (
              <div className="space-y-2 px-4 py-3 text-[13px]">
                <p className="text-[12px] text-muted">
                  Candles required for the scan: <span className="num">{ok(scan) ? (scanSummary.candles_required ?? row.initial_scan_candles) : "—"}</span> · configured{" "}
                  <span className="num">{row.initial_scan_candles}</span>
                </p>
                {scanSummary.counts ? (
                  <ul className="space-y-1 text-[12px]">
                    {Object.entries(scanSummary.counts).map(([key, value]) => (
                      <li key={key} className="flex items-baseline justify-between">
                        <span className="text-muted">{key.replace(/_/g, " ")}</span>
                        <span className="num">{value}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {scanSummary.ranked && scanSummary.ranked.length > 0 ? (
                  <p className="num text-[12px]">
                    {scanSummary.ranked
                      .map((symbol, index) => `${index + 1}. ${symbol}`)
                      .join("  ·  ")}
                  </p>
                ) : null}
              </div>
            ) : (
              <Empty>No initial scan journaled yet.</Empty>
            )}
            <div className="border-t border-line px-4 py-3 text-[11px] leading-snug text-muted">
              Want to see the effect of a threshold?{" "}
              <Link href="/watchlist" className="link">
                The watchlist
              </Link>{" "}
              shows each membership&rsquo;s own reason codes — failed ones are the ones a threshold is currently
              excluding.
            </div>
          </Panel>
        </div>
      )}

      <Panel title="What is not here" hint="and why">
        <ul className="grid gap-2 px-4 py-3 text-[12px] leading-snug text-ink-2 md:grid-cols-2">
          <li className="border-l-2 border-line-2 pl-3">
            No broker credentials. The keys that log in to the exchange belong to the worker process and are never
            configured here. Telegram is the exception the operator asked for on{" "}
            <Link href="/alerts" className="link">
              Alert rules
            </Link>
            : the bot token and chat id live in the database, are write-only on that page, and decide where level-break
            alerts are pushed.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            No mid-session override. A save writes a new versioned row that the worker reads when it opens its next
            session — the running session never changes rules under its own feet, and every save is journalled with who
            made it and what moved.
          </li>
        </ul>
      </Panel>
    </div>
  );
}

function Spec({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <dt className="text-[13px] font-medium">{label}</dt>
        <dd className="num text-[13px] font-semibold text-ink">{value}</dd>
      </div>
      <p className="mt-1 max-w-[60ch] text-[11px] leading-snug text-muted">{note}</p>
    </div>
  );
}
