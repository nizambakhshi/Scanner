import { clock, price, shares } from "@/lib/format";
import type { CandleRow, SnapshotRow } from "@/lib/types";

/**
 * One screen of the scanner's own arithmetic.
 *
 * Candles come from `candles_1m`, the three indicator lines from the
 * `indicator_snapshots` rows computed on the same candle, so a divergence between
 * a close and a VWAP on this chart is a real bug in the engine rather than a
 * rendering artifact. It is drawn as SVG with no charting dependency: the number
 * of series is fixed, the data is already in memory, and a bundling library would
 * only add a way for the two sources to disagree.
 *
 * Deliberate omissions: no "live" tick line and no projection of an unfinished
 * candle. The scanner only ever acts on a completed candle (§8), so drawing a
 * partially-formed bar would show the operator something the scanner refuses to
 * see.
 */
export function CandleChart({
  candles,
  snapshots,
  symbol,
  maxCandles = 120,
}: {
  candles: CandleRow[];
  snapshots: SnapshotRow[];
  symbol: string;
  maxCandles?: number;
}) {
  const shown = candles.slice(-maxCandles);
  if (shown.length < 2) {
    return (
      <p className="px-4 py-8 text-[13px] text-muted">
        {shown.length === 0
          ? `${symbol} has no completed 1-minute candles for this session — nothing was persisted, so nothing is drawn.`
          : `${symbol} has only ${shown.length} completed candle. EMA20 needs ${20}, so the scanner is still warming up.`}
      </p>
    );
  }

  const aligned = shown.map((candle) => snapshots.find((row) => row.candle_start_at === candle.candle_start_at) ?? null);

  const WIDTH = 960;
  const PRICE_HEIGHT = 300;
  const VOLUME_HEIGHT = 62;
  const PAD = { top: 12, right: 62, bottom: 22, left: 10 };
  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = PRICE_HEIGHT - PAD.top - PAD.bottom;

  const values: number[] = [];
  for (const [index, candle] of shown.entries()) {
    values.push(candle.high, candle.low);
    const snap = aligned[index];
    if (snap) {
      if (snap.vwap !== null) values.push(snap.vwap);
      if (snap.ema9 !== null) values.push(snap.ema9);
      if (snap.ema20 !== null) values.push(snap.ema20);
    }
  }
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    min = Math.min(...shown.map((candle) => candle.low));
    max = Math.max(...shown.map((candle) => candle.high));
    if (max <= min) max = min + 1;
  }
  const padValue = (max - min) * 0.06;
  min -= padValue;
  max += padValue;

  const step = plotWidth / shown.length;
  const bodyWidth = Math.max(1.5, Math.min(9, step * 0.62));
  const x = (index: number) => PAD.left + step * (index + 0.5);
  const y = (value: number) => PAD.top + ((max - value) / (max - min)) * plotHeight;

  const maxVolume = Math.max(...shown.map((candle) => candle.volume), 1);
  const volumeTop = PRICE_HEIGHT;
  const volumeBaseline = volumeTop + VOLUME_HEIGHT - 6;
  const vy = (value: number) => volumeBaseline - (value / maxVolume) * (VOLUME_HEIGHT - 12);

  // Grid: four price lines is enough to read a level; more turns into noise.
  const gridValues = [0, 0.25, 0.5, 0.75, 1].map((ratio) => max - ratio * (max - min));

  const line = (pick: (snap: SnapshotRow) => number | null) => {
    const segments: string[] = [];
    let current: string[] = [];
    shown.forEach((_, index) => {
      const snap = aligned[index];
      const value = snap ? pick(snap) : null;
      if (value === null) {
        if (current.length > 1) segments.push(current.join(" "));
        current = [];
        return;
      }
      current.push(`${x(index)},${y(value)}`);
    });
    if (current.length > 1) segments.push(current.join(" "));
    return segments;
  };

  const vwapSegments = line((snap) => snap.vwap);
  const ema9Segments = line((snap) => snap.ema9);
  const ema20Segments = line((snap) => snap.ema20);
  const last = shown[shown.length - 1];
  const lastSnap = aligned[aligned.length - 1];
  const first = shown[0];

  return (
    <div className="px-3 pb-3 pt-2">
      <svg viewBox={`0 0 ${WIDTH} ${PRICE_HEIGHT + VOLUME_HEIGHT}`} className="w-full" role="img" aria-label={`${symbol} 1-minute candles with VWAP, EMA9 and EMA20`}>
        {gridValues.map((value) => (
          <g key={value}>
            <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(value)} y2={y(value)} stroke="#e6e8eb" strokeWidth={1} />
            <text x={WIDTH - PAD.right + 6} y={y(value) + 4} className="num" fontSize={11} fill="#6a7078">
              {value.toFixed(2)}
            </text>
          </g>
        ))}

        {shown.map((candle, index) => {
          const up = candle.close >= candle.open;
          const colour = up ? "#0f8a5f" : "#d33131";
          const top = y(Math.max(candle.open, candle.close));
          const height = Math.max(1, Math.abs(y(candle.open) - y(candle.close)));
          const incomplete = !candle.is_complete;
          return (
            <g key={candle.candle_start_at} opacity={incomplete ? 0.45 : 1}>
              <line x1={x(index)} x2={x(index)} y1={y(candle.high)} y2={y(candle.low)} stroke={colour} strokeWidth={1} />
              <rect x={x(index) - bodyWidth / 2} y={top} width={bodyWidth} height={height} fill={up ? "#ffffff" : colour} stroke={colour} strokeWidth={1} />
              {candle.data_quality_status !== "OK" ? (
                <circle cx={x(index)} cy={PAD.top - 4} r={2.4} fill="#b4690e">
                  <title>{`${clock(candle.candle_start_at)} · ${candle.data_quality_status}`}</title>
                </circle>
              ) : null}
              <title>{`${clock(candle.candle_start_at)}  O ${candle.open}  H ${candle.high}  L ${candle.low}  C ${candle.close}  V ${candle.volume}${incomplete ? "  (incomplete)" : ""}`}</title>
            </g>
          );
        })}

        {vwapSegments.map((points, index) => (
          <polyline key={`vwap-${index}`} points={points} fill="none" stroke="#1d52f2" strokeWidth={1.6} />
        ))}
        {ema9Segments.map((points, index) => (
          <polyline key={`ema9-${index}`} points={points} fill="none" stroke="#7c3aed" strokeWidth={1.4} strokeDasharray="4 2" />
        ))}
        {ema20Segments.map((points, index) => (
          <polyline key={`ema20-${index}`} points={points} fill="none" stroke="#b4690e" strokeWidth={1.4} strokeDasharray="1 3" />
        ))}

        {shown.map((candle, index) => {
          const up = candle.close >= candle.open;
          return (
            <rect
              key={`vol-${candle.candle_start_at}`}
              x={x(index) - bodyWidth / 2}
              y={vy(candle.volume)}
              width={bodyWidth}
              height={Math.max(1, volumeBaseline - vy(candle.volume))}
              fill={up ? "#0f8a5f" : "#d33131"}
              opacity={0.28}
            />
          );
        })}
        <line x1={PAD.left} x2={WIDTH - PAD.right} y1={volumeBaseline} y2={volumeBaseline} stroke="#d2d6db" strokeWidth={1} />

        {[0, Math.floor(shown.length / 2), shown.length - 1].map((index) => (
          <text key={`t-${index}`} x={x(index)} y={PRICE_HEIGHT + VOLUME_HEIGHT - 4} fontSize={11} fill="#6a7078" className="num" textAnchor="middle">
            {clock(shown[index].candle_start_at)}
          </text>
        ))}
      </svg>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
        <Legend swatch="#1d52f2" label="session VWAP" />
        <Legend swatch="#7c3aed" label="EMA9" dashed />
        <Legend swatch="#b4690e" label="EMA20" dotted />
        <span className="num">
          {clock(first.candle_start_at)}–{clock(last.candle_start_at)} · {shown.length} candles
        </span>
        <span className="num">last {price(last.close)}</span>
        {lastSnap ? (
          <span className="num">
            vwap {price(lastSnap.vwap)} · ema9 {price(lastSnap.ema9)} · ema20 {price(lastSnap.ema20)}
          </span>
        ) : null}
        <span className="num">vol {shares(last.volume)}</span>
        {last.is_complete ? null : <span className="text-amber">last candle not yet complete</span>}
      </div>
    </div>
  );
}

function Legend({ swatch, label, dashed = false, dotted = false }: { swatch: string; label: string; dashed?: boolean; dotted?: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      <svg width={18} height={8} aria-hidden>
        <line
          x1={0}
          y1={4}
          x2={18}
          y2={4}
          stroke={swatch}
          strokeWidth={1.6}
          strokeDasharray={dashed ? "4 2" : dotted ? "1 3" : undefined}
        />
      </svg>
      {label}
    </span>
  );
}
