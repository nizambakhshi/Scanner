import Link from "next/link";
import { SectorChip } from "./SectorChip";
import type { SectorReads } from "@/lib/sector-reads";
import { SimChip } from "./SimulatedBanner";
import { DeliveryBadge, SignalBadge, StrategyChip } from "./badges";
import { Empty } from "./ui";
import { clock, multiple, pct, price, score } from "@/lib/format";
import { sectorFor } from "@/lib/sector-map";
import type { DeliveryRow, SignalRow } from "@/lib/types";

/**
 * The alert feed, with delivery status beside it.
 *
 * A signal and a delivered alert are different facts: the queue suppresses a
 * duplicate inside the cooldown window, retries a failed send, and can exhaust its
 * attempts entirely. Showing the signal alone would tell an operator "there was a
 * setup" while they sat waiting for a notification that never left the process, so
 * each row carries its Telegram row (or the honest "no channel").
 */
export function SignalsTable({
  signals,
  deliveries = [],
  compact = false,
  sectorReads,
}: {
  signals: SignalRow[];
  deliveries?: DeliveryRow[];
  compact?: boolean;
  /** The sector indices' readings for first paint; the chips then keep
   *  themselves current off the shared poller. */
  sectorReads?: SectorReads;
}) {
  if (signals.length === 0) return <Empty>No signals for this filter.</Empty>;
  const bySignal = new Map<number, DeliveryRow[]>();
  for (const row of deliveries) {
    const bucket = bySignal.get(row.signal_id);
    if (bucket) bucket.push(row);
    else bySignal.set(row.signal_id, [row]);
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line">
            <th className="th">Time</th>
            <th className="th">Symbol</th>
            <th className="th">Signal</th>
            <th className="th text-right">Price</th>
            <th className="th hidden text-right md:table-cell">Day chg</th>
            <th className="th hidden text-right lg:table-cell">Rel vol</th>
            <th className="th hidden text-right md:table-cell">Score</th>
            {compact ? null : <th className="th hidden text-right lg:table-cell">vs VWAP</th>}
            {compact ? null : <th className="th hidden xl:table-cell">Message</th>}
            <th className="th">Delivery</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((signal) => {
            const telegram = bySignal.get(signal.id)?.find((row) => row.channel === "TELEGRAM");
            const href = signal.symbol ? `/instrument/${encodeURIComponent(signal.symbol)}` : null;
            const sector = sectorFor(signal.symbol);
            return (
              <tr key={`${signal.id}-${signal.signal_type}`} className="row-hover border-b border-line last:border-0">
                <td className="td">
                  <div className="num text-[12px]">{clock(signal.event_timestamp)}</div>
                  <div className="num text-[11px] text-muted">
                    {signal.candle_start_at ? `candle ${clock(signal.candle_start_at)}` : "—"}
                  </div>
                </td>
                <td className="td">
                  {href ? (
                    <Link href={href} className="num font-semibold link">
                      {signal.symbol ?? "session"}
                    </Link>
                  ) : (
                    <span className="num font-semibold">{signal.symbol ?? "session"}</span>
                  )}
                  <div className="mt-0.5 flex flex-wrap items-center gap-1">
                    <SectorChip sector={sector} initial={sector ? (sectorReads?.[sector] ?? null) : null} />
                    <SimChip value={signal.is_simulated} />
                  </div>
                </td>
                <td className="td">
                  <SignalBadge type={signal.signal_type} />
                  <div className="mt-1 flex flex-col gap-0.5">
                    <StrategyChip reasons={signal.reasons} />
                    <span className="num text-[11px] text-muted">cfg v{signal.config_version}</span>
                  </div>
                </td>
                <td className="td num text-right">
                  {price(signal.price)}
                  <div className="num text-[11px] text-muted">
                    {signal.previous_close === null ? "" : `prev ${price(signal.previous_close)}`}
                  </div>
                </td>
                <td className={`td hidden num text-right md:table-cell ${signal.intraday_change_pct && signal.intraday_change_pct < 0 ? "text-negative" : "text-positive"}`}>
                  {pct(signal.intraday_change_pct)}
                </td>
                <td className="td hidden num text-right lg:table-cell">{multiple(signal.relative_volume)}</td>
                <td className="td hidden num text-right md:table-cell">
                  {score(signal.score)}
                  <div className="num text-[11px] text-muted">{signal.rank === null ? "" : `rank ${signal.rank}`}</div>
                </td>
                {compact ? null : (
                  <td className="td hidden num text-right lg:table-cell">
                    {pct(
                      signal.price !== null && signal.vwap !== null
                        ? ((signal.price - signal.vwap) / signal.vwap) * 100
                        : null,
                    )}
                    <div className="num text-[11px] text-muted">{signal.vwap === null ? "" : price(signal.vwap)}</div>
                  </td>
                )}
                {compact ? null : (
                  <td className="td hidden max-w-[280px] xl:table-cell">
                    <p className="text-[12px] leading-snug text-ink-2">{signal.message ?? reasonList(signal.reasons)}</p>
                  </td>
                )}
                <td className="td">
                  <DeliveryBadge
                    status={telegram?.delivery_status ?? signal.telegram_status}
                    attempts={telegram?.attempt_count ?? signal.telegram_attempts}
                    simulated={signal.is_simulated || (telegram?.is_simulated ?? false)}
                  />
                  {telegram?.last_error ? (
                    <div className="mt-1 max-w-[220px] break-words text-[11px] text-negative">{telegram.last_error}</div>
                  ) : signal.telegram_error ? (
                    <div className="mt-1 max-w-[220px] break-words text-[11px] text-negative">{signal.telegram_error}</div>
                  ) : null}
                  {telegram?.next_retry_at ? (
                    <div className="num mt-1 text-[11px] text-amber">retry {clock(telegram.next_retry_at)}</div>
                  ) : null}
                  {telegram?.delivered_at ? (
                    <div className="num mt-1 text-[11px] text-muted">sent {clock(telegram.delivered_at)}</div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function reasonList(reasons: string[]): string {
  if (reasons.length === 0) return "—";
  return reasons.map((reason) => reason.toLowerCase().replace(/_/g, " ")).join(" · ");
}
