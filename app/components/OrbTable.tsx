import Link from "next/link";
import { Empty } from "./ui";
import { clock, orDash, price } from "@/lib/format";
import { ORB_VOLUME_MULTIPLE } from "@/lib/orb";
import type { OrbStatus, OrbView } from "@/lib/types";

/**
 * The ORB board: every stock's opening range, first breakout, and the three
 * confirmation readings the strategy's filters name. Read-only by design — the
 * table shows what the stored 1-minute candles say, and the scanner never places
 * orders. A dash always means "the session has not produced this yet", never 0.
 */
export function OrbTable({ views }: { views: OrbView[] }) {
  if (views.length === 0) {
    return <Empty>No 1-minute candles for any universe instrument yet — the board fills in from the stored session bars.</Empty>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line">
            <th className="th">Symbol</th>
            <th className="th text-right" title="High and low of the first five minutes (9:15–9:20 IST)">
              Opening range
            </th>
            <th className="th">Status</th>
            <th className="th text-right" title="The first 5-minute candle to close beyond the range, and its volume against the session's average 5-minute candle">
              Breakout candle
            </th>
            <th className="th text-right" title="Session VWAP (Σ(TP·V)/ΣV). The strategy wants longs above it, shorts below it.">
              VWAP
            </th>
            <th className="th text-right" title="1-minute EMA 9 and 20 from the newest worker snapshot — the strategy wants them agreeing with the direction">
              EMA 9/20
            </th>
            <th className="th text-right" title="Stop = opposite end of the range; T1/T2 = 1× and 2× the range width from the broken edge">
              Stop / Targets
            </th>
          </tr>
        </thead>
        <tbody>
          {views.map((view) => (
            <OrbRow key={view.row.instrument_id} view={view} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OrbRow({ view }: { view: OrbView }) {
  const { row } = view;
  const breakoutAt = view.direction === "UP" ? row.bu_at : view.direction === "DOWN" ? row.bd_at : null;
  const breakoutClose = view.direction === "UP" ? row.bu_close : view.direction === "DOWN" ? row.bd_close : null;
  const ratio = view.direction === "UP" ? row.bu_ratio : view.direction === "DOWN" ? row.bd_ratio : null;

  return (
    <tr className="row-hover border-b border-line last:border-0">
      <td className="td">
        <Link href={`/instrument/${encodeURIComponent(row.symbol)}`} className="num font-semibold link">
          {row.symbol}
        </Link>
        <div className="mt-0.5 max-w-[220px] truncate text-[11px] text-muted">{orDash(row.company_name)}</div>
      </td>
      <td className="td num text-right">
        {view.width === null ? "—" : `${price(row.or_low)} – ${price(row.or_high)}`}
        <div className="text-[11px] text-muted">
          {view.width_pct === null ? "" : `${view.width_pct.toFixed(2)}% wide`}
        </div>
      </td>
      <td className="td">
        <StatusChip status={view.status} />
        {breakoutAt ? <div className="num mt-0.5 text-[11px] text-muted">broke {clock(breakoutAt)}</div> : null}
        {view.retreated ? <div className="mt-0.5 text-[11px] text-amber">back inside the range</div> : null}
      </td>
      <td className="td num text-right">
        {breakoutClose === null ? (
          <span className="text-muted" title="No 5-minute close beyond the range yet">
            —
          </span>
        ) : (
          <>
            {price(breakoutClose)}
            <div className="num text-[11px] text-muted">{breakoutAt ? clock(breakoutAt) : ""}</div>
            <div className="mt-0.5 flex justify-end">
              <VolumeRatioChip ratio={ratio} />
            </div>
          </>
        )}
      </td>
      <td className="td num text-right">
        {price(row.session_vwap)}
        {view.vwap_side ? (
          <div className="mt-0.5 flex justify-end">
            <span
              title={
                view.direction === null
                  ? "No breakout direction to judge yet"
                  : view.vwap_agrees
                    ? "Price is on the side the strategy's filter wants"
                    : "Price is on the wrong side of VWAP for this direction"
              }
              className={`chip ${tone(vwapTone(view))}`}
            >
              {view.vwap_side === "above" ? "price above" : "price below"}
            </span>
          </div>
        ) : null}
      </td>
      <td className="td num text-right">
        {row.ema9 === null || row.ema20 === null ? (
          <span className="text-muted" title="No worker snapshot for this symbol yet — the EMA carries across sessions and is only written while the worker scans">
            —
          </span>
        ) : (
          <>
            {fmt(row.ema9)} · {fmt(row.ema20)}
            <div className="mt-0.5 flex justify-end">
              <span
                title={
                  view.direction === null
                    ? "No breakout direction to judge yet"
                    : view.ema_agrees
                      ? "The 9/20 EMA agrees with the direction"
                      : "The 9/20 EMA does not agree with the direction"
                }
                className={`chip ${tone(emaTone(view))}`}
              >
                {row.ema9 > row.ema20 ? "9 above 20" : row.ema9 < row.ema20 ? "9 below 20" : "9 = 20"}
              </span>
            </div>
          </>
        )}
      </td>
      <td className="td num text-right">
        {view.direction === null ? (
          <span className="text-muted" title="Levels appear when a 5-minute close leaves the range">
            —
          </span>
        ) : (
          <>
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted">SL </span>
              {price(view.stop)}
            </div>
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted">T1 </span>
              {price(view.target1)}
            </div>
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted">T2 </span>
              {price(view.target2)}
            </div>
          </>
        )}
      </td>
    </tr>
  );
}

const STATUS_STYLES: Record<OrbStatus, string> = {
  BROKE_UP: "border-positive/40 bg-positive/10 text-positive",
  BROKE_DOWN: "border-negative/40 bg-negative/10 text-negative",
  INSIDE: "border-line-2 bg-surface-2 text-ink-2",
  OPENING: "border-amber/40 bg-amber/10 text-amber",
  NO_DATA: "border-line-2 bg-surface-2 text-muted",
};

const STATUS_LABELS: Record<OrbStatus, string> = {
  BROKE_UP: "Broke up",
  BROKE_DOWN: "Broke down",
  INSIDE: "Inside range",
  OPENING: "Opening range",
  NO_DATA: "No data",
};

function StatusChip({ status }: { status: OrbStatus }) {
  return <span className={`chip ${STATUS_STYLES[status]}`}>{STATUS_LABELS[status]}</span>;
}

function VolumeRatioChip({ ratio }: { ratio: number | null }) {
  if (ratio === null) {
    return (
      <span className="text-[11px] text-muted" title="No prior 5-minute volume to average yet">
        vol —
      </span>
    );
  }
  const pass = ratio >= ORB_VOLUME_MULTIPLE;
  return (
    <span
      title={`Breakout candle traded ${ratio.toFixed(2)}× the session's average 5-minute volume (filter: ≥ ${ORB_VOLUME_MULTIPLE}×)`}
      className={`chip ${tone(pass ? "positive" : "amber")}`}
    >
      {ratio.toFixed(2)}× avg
    </span>
  );
}

type Tone = "positive" | "negative" | "amber" | "neutral";

function tone(value: Tone): string {
  return value === "positive"
    ? "border-positive/40 bg-positive/10 text-positive"
    : value === "negative"
      ? "border-negative/40 bg-negative/10 text-negative"
      : value === "amber"
        ? "border-amber/40 bg-amber/10 text-amber"
        : "border-line-2 bg-surface-2 text-muted";
}

function vwapTone(view: OrbView): Tone {
  if (view.direction === null) return "neutral";
  return view.vwap_agrees ? "positive" : "negative";
}

function emaTone(view: OrbView): Tone {
  if (view.direction === null) return "neutral";
  if (view.ema_agrees === null) return "neutral";
  return view.ema_agrees ? "positive" : "negative";
}

/** EMA values are prices; two full ₹ prices would not fit, so keep the raw
 *  two-decimal shape — the same numbers the snapshot store holds. */
function fmt(value: number): string {
  return value.toFixed(2);
}
