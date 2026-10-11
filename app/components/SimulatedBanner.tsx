import type { SourceMeta } from "@/lib/types";

/**
 * The most important component in the app.
 *
 * A replayed candle looks exactly like a real one. The only defence against an
 * operator trading on synthetic data is a statement they cannot scroll past, so
 * this renders whenever the source is the offline sample, and the per-row
 * `SIM` chips below it render whenever an individual row carries `is_simulated`.
 */
export function SimulatedBanner({ meta }: { meta: SourceMeta }) {
  if (!meta.simulated) return null;
  return (
    <div className="border-b border-sim/30 bg-sim/10">
      <div className="mx-auto flex max-w-[1400px] flex-col gap-1 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-3">
        <span className="chip border-sim/40 bg-sim/15 text-sim">Simulated replay</span>
        <p className="text-[13px] leading-snug text-ink-2">
          {meta.banner ??
            "Synthetic ticks replayed through the scanner pipeline. Not market data — do not trade on anything on this screen."}
        </p>
        {meta.tradingDate ? <span className="num text-[12px] text-muted">session {meta.tradingDate}</span> : null}
      </div>
    </div>
  );
}

/** Row-level marker: one candle, one signal, one membership can be simulated while
 *  the rest of the table is not. */
export function SimChip({ value, label = "SIM" }: { value: boolean; label?: string }) {
  if (!value) return null;
  return (
    <span title="This row was produced by the offline replay" className="chip border-sim/30 bg-sim/10 text-sim">
      {label}
    </span>
  );
}
