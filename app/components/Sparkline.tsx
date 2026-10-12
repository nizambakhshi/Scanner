import { price } from "@/lib/format";
import { sparklineGeometry, type SparklineTone } from "@/lib/sparkline";

/**
 * A server-rendered sparkline: one SVG path and one dot, no client JS, no chart
 * library. Rows that have no series render a dash with the reason attached —
 * the column must never imply a trend that was not measured.
 */

const TONE_COLOR: Record<SparklineTone, string> = {
  positive: "var(--color-positive)",
  negative: "var(--color-negative)",
  muted: "var(--color-muted-2)",
};

const TONE_WORD: Record<SparklineTone, string> = {
  positive: "up",
  negative: "down",
  muted: "unchanged",
};

export function Sparkline({
  values,
  width = 96,
  height = 24,
  missing = "No series synced yet",
}: {
  values?: readonly number[];
  width?: number;
  height?: number;
  missing?: string;
}) {
  const geometry = values ? sparklineGeometry(values, width, height) : null;
  if (!geometry) {
    return (
      <span className="text-[11px] text-muted-2" title={missing}>
        —
      </span>
    );
  }
  const stroke = TONE_COLOR[geometry.tone];
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Trend ${TONE_WORD[geometry.tone]}: ${values?.length ?? 0} closes from ${price(geometry.first)} to ${price(geometry.last)}`}
      className="block"
    >
      <path d={geometry.path} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={geometry.lastX} cy={geometry.lastY} r={1.8} fill={stroke} />
    </svg>
  );
}
