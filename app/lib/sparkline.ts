/**
 * The geometry behind the tiny table glyphs.
 *
 * A sparkline here is a trend statement, not an axis: no labels, no grid, one
 * path. The tone carries the only meaning it adds — where the newest close sits
 * relative to the oldest — green up, red down, dim when there is no direction to
 * claim. Nothing is interpolated and nothing is padded: a bucket with no trades
 * simply does not produce a point, so the glyph is exactly as dense as the data.
 */

export type SparklineTone = "positive" | "negative" | "muted";

export type SparklineGeometry = {
  /** SVG path data — "M… L… L…" spanning the full box. */
  path: string;
  /** The newest point, for the "where it is now" dot. */
  lastX: number;
  lastY: number;
  tone: SparklineTone;
  first: number;
  last: number;
};

export function sparklineGeometry(
  values: readonly number[],
  width = 96,
  height = 24,
): SparklineGeometry | null {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const pad = 2;
  const usableW = Math.max(1, width - pad * 2);
  const usableH = Math.max(1, height - pad * 2);
  const x = (index: number) => pad + (index / (values.length - 1)) * usableW;
  const y = (value: number) => (span === 0 ? height / 2 : pad + ((max - value) / span) * usableH);

  const parts: string[] = [];
  values.forEach((value, index) => {
    parts.push(`${index === 0 ? "M" : "L"}${x(index).toFixed(2)} ${y(value).toFixed(2)}`);
  });

  const first = values[0];
  const last = values[values.length - 1];
  // A close that lands exactly where it started — or never moved — has no
  // direction, and the glyph must not invent one.
  const tone: SparklineTone = span === 0 || last === first ? "muted" : last > first ? "positive" : "negative";

  return {
    path: parts.join(" "),
    lastX: x(values.length - 1),
    lastY: y(last),
    tone,
    first,
    last,
  };
}
