/**
 * Display + normalisation helpers.
 *
 * India-market convention throughout: prices in rupees with two decimals,
 * quantities in lakhs only when the number is too long to read, and every clock
 * is Asia/Kolkata. The database stores `TIMESTAMPTZ`, so a value that reaches a
 * component has already been converted here — a component must never do timezone
 * arithmetic, because "the candle at 09:15" and "the candle at 09:15 UTC" are the
 * same row rendered as two different truths.
 */

// Asia/Kolkata is UTC+05:30 year-round: no DST, so a fixed offset is exact.
const istFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** `Date | ISO string | null` → `YYYY-MM-DDTHH:MM:SS+05:30`, or null. */
export function toIst(value: Date | string | number | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts: Record<string, string> = {};
  for (const part of istFormatter.formatToParts(date)) parts[part.type] = part.value;
  // `24` appears for midnight in some ICU versions; the ISO form wants `00`.
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}:${parts.second}+05:30`;
}

export function isValidIst(iso: string | null | undefined): boolean {
  if (!iso) return false;
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/.test(iso);
}

/** `2026-10-12T09:15:00+05:30` → `09:15`. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const time = iso.slice(11, 16);
  return time === "" ? "—" : time;
}

/** `2026-10-12T09:15:00+05:30` → `12 Oct 2026 · 09:15`. */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [day, month, year] = [iso.slice(8, 10), iso.slice(5, 7), iso.slice(0, 4)];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const label = months[Number(month) - 1] ?? month;
  return `${day} ${label} ${year} · ${iso.slice(11, 16)}`;
}

/** `2026-10-12` → `Mon 12 Oct 2026`. Dates carry no timezone, so none is invented. */
export function longDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [year, month, day] = [Number(iso.slice(0, 4)), Number(iso.slice(5, 7)), Number(iso.slice(8, 10))];
  if (!year || !month || !day) return iso;
  const weekday = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${weekday} ${String(day).padStart(2, "0")} ${months[month - 1]} ${year}`;
}

/** Seconds → `2m 30s`, `1h 04m` — the staleness figure an operator reads at a glance. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/**
 * Minutes elapsed since `iso`, negative for the future. Takes the IST ISO string
 * this module produces, so "is the worker still beating?" needs no timezone guess.
 */
export function minutesSince(iso: string | null | undefined, now: number = Date.now()): number | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return (now - then) / 60_000;
}

export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  const minutes = minutesSince(iso, now);
  if (minutes === null) return "never";
  if (minutes < 0.5) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  if (minutes < 60 * 12) return `${Math.round(minutes / 60)} h ago`;
  return dateTime(iso);
}

// ── value coercion (both sources normalise before a row leaves the data layer) ─

/** `NUMERIC` arrives as a string over the PostgreSQL wire; the domain is `number`. */
export function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number.parseFloat(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}

export function int(value: unknown, fallback = 0): number {
  if (value === null || value === undefined || value === "") return fallback;
  const parsed = typeof value === "number" ? value : Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function bool(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (value === 1 || value === "1" || value === "t" || value === "true") return true;
  return false;
}

export function strings(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (typeof value === "string") {
    // Postgres `TEXT[]` rendered as a literal: `{a,b}` — only the replay path
    // should need this, but a stray string must not become one giant reason.
    const trimmed = value.replace(/^\{|\}$/g, "");
    return trimmed === "" ? [] : trimmed.split(",");
  }
  return [];
}

export function jsonRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object") return value as Record<string, unknown>;
  if (typeof value === "string") {
    try {
      return JSON.parse(value) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return {};
}

// ── money / metrics ───────────────────────────────────────────────────────────

export function price(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `₹${value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function multiple(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}×`;
}

export function shares(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 10_000_000) return `${(value / 10_000_000).toFixed(2)} Cr`;
  if (value >= 100_000) return `${(value / 100_000).toFixed(2)} L`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

export function score(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(1);
}

export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/** `WATCHLIST_ADDED` → `Watchlist added`; `DATA_FEED_INTERRUPTED` → `Data feed interrupted`. */
export function humanise(value: string | null | undefined): string {
  if (!value) return "—";
  const words = value.split("_").filter((word) => word !== "");
  if (words.length === 0) return value;
  return words
    .map((word, index) => (index === 0 ? word.charAt(0) + word.slice(1).toLowerCase() : word.toLowerCase()))
    .join(" ");
}

/** Session-unique key for a row in a table; keeps React keys honest without ids. */
export function rowKey(prefix: string, value: unknown, fallback: number): string {
  const middle = value === null || value === undefined ? "row" : String(value);
  return `${prefix}-${middle}-${fallback}`;
}
