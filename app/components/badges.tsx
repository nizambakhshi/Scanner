import type { RsiBias, SignalType, WatchlistState } from "@/lib/types";
import { strategyLabel } from "@/lib/alert-strategies";
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
  RSI_ENTRY: "border-positive/40 bg-positive/10 text-positive",
  RSI_OVERBOUGHT: "border-negative/40 bg-negative/10 text-negative",
  // Retired 2026-10-15, and deliberately grey rather than re-coloured: an old row
  // labelled "Rsi Bullish" was raised for RSI above 60, which the current rule calls
  // the red side. Painting it green would make the feed disagree with every other
  // number on the board; muting it says "history, not news".
  RSI_BULLISH: "border-line-2 bg-surface-2 text-muted",
  RSI_BEARISH: "border-line-2 bg-surface-2 text-muted",
  BREAKOUT_UP: "border-positive/40 bg-positive/10 text-positive",
  BREAKDOWN: "border-negative/40 bg-negative/10 text-negative",
};

/** Types whose spelled-out form loses the acronym an operator reads on a phone:
 *  `humanise("RSI_ENTRY")` is "Rsi entry", which nobody recognises as the alert they
 *  configured. These are the same words the worker hand-writes into the Telegram
 *  message, so the feed and the notification spell the event identically. */
const SIGNAL_LABELS: Partial<Record<SignalType, string>> = {
  RSI_ENTRY: "RSI entry",
  RSI_OVERBOUGHT: "RSI overbought",
  RSI_BULLISH: "RSI bullish",
  RSI_BEARISH: "RSI bearish",
};

export function SignalBadge({ type }: { type: SignalType }) {
  const style = SIGNAL_STYLES[type] ?? "border-line-2 bg-surface-2 text-ink-2";
  return <span className={`chip ${style}`}>{SIGNAL_LABELS[type] ?? humanise(type)}</span>;
}

/**
 * Which of the four rules raised this row.
 *
 * The discriminator is `strategy:<name>`, first in `reasons`, because that is the
 * only column a signal row has for it: `BREAKOUT_UP` from the opening range and
 * `BREAKOUT_UP` from a VWAP reclaim are different news even when the stock, the
 * minute and the type match. Without this chip the feed would claim two alerts were
 * the same event.
 */
export function StrategyChip({ reasons }: { reasons: string[] }) {
  const code = reasons.find((reason) => reason.startsWith("strategy:"));
  if (!code) return null;
  const key = code.slice("strategy:".length);
  const label = strategyLabel(key) ?? humanise(key);
  return (
    <span
      title={`Raised by the ${label} rule on /alerts`}
      className="font-display text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-2"
    >
      {label}
    </span>
  );
}

/** The three sides of the operator's rule (2026-10-15), spelled the same in the
 *  database, on this board and in a Telegram message: **ENTRY** is the green side —
 *  RSI under 40, the dip worth acting on — and **OVERBOUGHT** the red one, above 60.
 *  The old words described momentum and pointed these colours the other way, so the
 *  words changed rather than the colours. */
const SIDE_STYLE: Record<NonNullable<RsiBias>, string> = {
  ENTRY: "border-positive/40 bg-positive/10 text-positive",
  OVERBOUGHT: "border-negative/40 bg-negative/10 text-negative",
  NEUTRAL: "border-line-2 bg-surface-2 text-muted",
};

/** Two horizons measure the same rule and must not be described with one sentence:
 *  the nightly pair of timeframes says nothing about today's tape, and the
 *  five-minute reading says nothing about the weekly trend. */
const NIGHTLY_MEANING: Record<NonNullable<RsiBias>, string> = {
  ENTRY: "Daily and weekly RSI-14 are both below 40 — the oversold side of the pair.",
  OVERBOUGHT: "Daily and weekly RSI-14 are both above 60 — the expensive side of the pair.",
  NEUTRAL: "The daily and weekly readings are not both under 40 or both above 60.",
};

const INTRADAY_MEANING: Record<NonNullable<RsiBias>, string> = {
  ENTRY: "RSI-14 on today's five-minute closes is under 40 while the live trend is up — a dip inside an up-trend.",
  OVERBOUGHT: "RSI-14 on today's five-minute closes is above 60 while the live trend is down — an expensive print inside a down-trend.",
  NEUTRAL: "Inside the 40–60 band, or an extreme the live trend does not confirm.",
};

/** The two-timeframe RSI classification. `null` is "not classified yet" — the
 *  daily/weekly sync has not covered this symbol — and it must not read as neutral,
 *  because neutral is a measurement. */
export function RsiBiasBadge({ bias }: { bias: RsiBias }) {
  if (!bias) {
    return (
      <span className="text-[11px] text-muted" title="No RSI classification yet — the daily/weekly sync has not covered this symbol">
        —
      </span>
    );
  }
  return (
    <span title={NIGHTLY_MEANING[bias]} className={`chip ${SIDE_STYLE[bias]}`}>
      {humanise(bias)}
    </span>
  );
}

/**
 * The intraday verdict, judged once per five-minute bucket by the worker's `rsi`
 * detector and stored on the candle it was measured on.
 *
 * It is a *state*, not a threshold crossing: the row carries the same word for every
 * minute of the bucket it was decided in, which is why this badge shows the band and
 * the trend it was judged against rather than a number. `null` is "no reading yet" —
 * a session shorter than fifteen buckets, or a restart before the next close — and
 * renders as an em dash for the same reason the nightly badge does: inventing
 * NEUTRAL for a tape nobody measured would make an absence look like a verdict.
 */
export function RsiStateBadge({ state, reading = null }: { state: RsiBias; reading?: number | null }) {
  if (!state) {
    return (
      <span className="text-[11px] text-muted" title="No five-minute RSI reading yet — the session needs fifteen buckets">
        —
      </span>
    );
  }
  return (
    <span title={INTRADAY_MEANING[state]} className={`chip ${SIDE_STYLE[state]}`}>
      {humanise(state)}
      {reading !== null && <span className="num ml-1 opacity-80">{reading.toFixed(1)}</span>}
    </span>
  );
}

/** Tone for a single RSI reading against the operator's two lines. Green is under 40
 *  because that is the side the rule calls an entry, red is over 60 because that is
 *  the one it warns about — a reading is coloured by what it means to do, not by
 *  which way the number went. */
export function rsiBandTone(value: number | null): string {
  if (value === null) return "text-muted";
  if (value < 40) return "text-positive";
  if (value > 60) return "text-negative";
  return "";
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
