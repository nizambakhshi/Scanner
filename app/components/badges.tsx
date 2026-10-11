import type { SignalType, WatchlistState } from "@/lib/types";
import { humanise } from "@/lib/format";

const STATE_STYLES: Record<WatchlistState, string> = {
  ACTIVE: "border-positive/40 bg-positive/10 text-positive",
  REQUALIFIED: "border-accent/40 bg-accent/10 text-accent",
  WEAKENING: "border-amber/40 bg-amber/10 text-amber",
  DATA_STALE: "border-amber/40 bg-amber/10 text-amber",
  WARMING_UP: "border-line-2 bg-surface-2 text-muted",
  REMOVED: "border-line-2 bg-surface-2 text-muted",
};

const STATE_MEANING: Record<WatchlistState, string> = {
  ACTIVE: "All bullish conditions hold on the latest completed candle.",
  REQUALIFIED: "Qualified again after being removed — a new episode.",
  WEAKENING: "Failed a candle; removal needs the full confirmation streak.",
  DATA_STALE: "Feed exceeded the staleness threshold — state changes are suspended.",
  WARMING_UP: "Not enough history yet; the scanner refuses to guess an EMA.",
  REMOVED: "Left the watchlist after the confirmation streak.",
};

export function StateBadge({ state }: { state: WatchlistState }) {
  const style = STATE_STYLES[state] ?? "border-line-2 bg-surface-2 text-muted";
  return (
    <span title={STATE_MEANING[state]} className={`chip ${style}`}>
      {humanise(state)}
    </span>
  );
}

const SIGNAL_STYLES: Partial<Record<SignalType, string>> = {
  BULLISH_ENTRY: "border-positive/40 bg-positive/10 text-positive",
  WATCHLIST_REQUALIFIED: "border-accent/40 bg-accent/10 text-accent",
  WATCHLIST_WEAKENING: "border-amber/40 bg-amber/10 text-amber",
  WATCHLIST_REMOVED: "border-line-2 bg-surface-2 text-muted",
  DATA_FEED_INTERRUPTED: "border-negative/40 bg-negative/10 text-negative",
  DATA_FEED_RECOVERED: "border-positive/40 bg-positive/10 text-positive",
};

export function SignalBadge({ type }: { type: SignalType }) {
  const style = SIGNAL_STYLES[type] ?? "border-line-2 bg-surface-2 text-ink-2";
  return <span className={`chip ${style}`}>{humanise(type)}</span>;
}

/** Telegram is the only channel a person actually receives; its queue status is
 *  the difference between "no alert" and "alert not delivered". */
export function DeliveryBadge({
  status,
  attempts,
  simulated,
}: {
  status: string | null;
  attempts: number;
  simulated: boolean;
}) {
  if (simulated) {
    return (
      <span title="Simulation mode never contacts a provider" className="chip border-sim/30 bg-sim/10 text-sim">
        Not sent (simulated)
      </span>
    );
  }
  if (!status) {
    return <span className="chip border-line-2 bg-surface-2 text-muted">No channel</span>;
  }
  const tone =
    status === "DELIVERED"
      ? "border-positive/40 bg-positive/10 text-positive"
      : status === "PERMANENTLY_FAILED"
        ? "border-negative/40 bg-negative/10 text-negative"
        : status === "SUPPRESSED"
          ? "border-line-2 bg-surface-2 text-muted"
          : "border-amber/40 bg-amber/10 text-amber";
  const suffix = attempts > 1 ? ` · ${attempts} tries` : "";
  return <span className={`chip ${tone}`}>{humanise(status)}{suffix}</span>;
}
