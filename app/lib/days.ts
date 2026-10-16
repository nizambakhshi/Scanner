/**
 * The trading-day exceptions — the "Days" module the /days page owns.
 *
 * The NSE calendar this scanner runs on is Mon–Fri; exchange holidays and half
 * days are not in any table this project controls. Rather than guess a holiday
 * list (market-clock.ts makes exactly that point about the session strip), the
 * operator states the exceptions: a list of weekdays to *skip*, and a list to
 * force open. Everything else stays the weekday rule.
 *
 * The document lives in `ops_settings['days']`, written by the dashboard and
 * read by the worker every `ALERT_SETTINGS_POLL_SECONDS` — the same arrangement
 * as the two `/alerts` keys. The worker's gate is at the session-open decision
 * (see `WorkerRuntime`), so an exception saved at 08:00 is obeyed at 09:15.
 *
 * Validation is shared here so the page and the route cannot disagree: a date
 * must be a real `YYYY-MM-DD` calendar day, lists are sorted and deduped, and
 * a date in both lists is refused — it is the one document that can contradict
 * itself, and a silent precedence rule would hide that from the operator.
 */

export const DAYS_SETTINGS_KEY = "days";

/** One exception: a date and why the operator marked it ("Republic Day"). */
export type DayEntry = { date: string; note: string | null };

/** What the page edits and the worker obeys. `off_days` skips, `on_days` forces open. */
export type DaysDoc = { version: 1; off_days: DayEntry[]; on_days: DayEntry[] };

export type DaysState = {
  doc: DaysDoc;
  /** `saved` — a row the worker will read; `default` — the plain weekday rule. */
  source: "saved" | "default";
  updatedAt: string | null;
  lastChange: { at: string | null; actor: string | null; summary: string | null } | null;
};

/** The shipped state: no exceptions, Monday–Friday. */
export function defaultDays(): DaysDoc {
  return { version: 1, off_days: [], on_days: [] };
}

/** Is `value` a real Gregorian calendar date spelled `YYYY-MM-DD`? */
export function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [y, m, d] = value.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const daysInMonth = [31, (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0 ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return d <= daysInMonth[m - 1];
}

/**
 * Parse the stored document or a submission. Malformed entries are *named* in
 * the error, not dropped: a page that quietly discarded "2026-13-01" would show
 * a calendar the worker then refuses to obey.
 */
export function parseDays(document: unknown): { ok: true; doc: DaysDoc } | { ok: false; error: string } {
  if (document === null || document === undefined) return { ok: true, doc: defaultDays() };
  if (typeof document !== "object" || Array.isArray(document)) {
    return { ok: false, error: "the days document must be an object" };
  }
  const value = document as { off_days?: unknown; on_days?: unknown };
  const read = (list: unknown, field: string): DayEntry[] | { ok: false; error: string } => {
    if (list === undefined || list === null) return [];
    if (!Array.isArray(list)) {
      return { ok: false, error: `days.${field} must be a list of {date, note} entries.` };
    }
    const entries: DayEntry[] = [];
    const seen = new Set<string>();
    for (const raw of list) {
      const entry = (raw ?? {}) as { date?: unknown; note?: unknown };
      if (!isIsoDate(entry.date)) {
        return {
          ok: false,
          error: `"${describe(raw)}" is not a trading-day date — use YYYY-MM-DD, like 2026-10-26.`,
        };
      }
      if (seen.has(entry.date)) continue; // a repeated exception is the same exception
      seen.add(entry.date);
      const note = typeof entry.note === "string" && entry.note.trim() !== "" ? entry.note.trim().slice(0, 80) : null;
      entries.push({ date: entry.date, note });
    }
    return entries.sort((a, b) => (a.date < b.date ? -1 : 1));
  };
  const off = read(value.off_days, "off_days");
  if (!Array.isArray(off)) return off;
  const on = read(value.on_days, "on_days");
  if (!Array.isArray(on)) return on;
  const contradiction = off.filter((day) => on.some((other) => other.date === day.date));
  if (contradiction.length > 0) {
    return {
      ok: false,
      error: `${contradiction.map((day) => day.date).join(", ")} is listed both as a skip and as a forced-open day. Remove it from one list.`,
    };
  }
  return { ok: true, doc: { version: 1, off_days: off, on_days: on } };
}

function describe(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return JSON.stringify(value).slice(0, 60);
  return String(value);
}

/** UTC-day arithmetic on the ISO string itself: no clock, no locale, no DST (India has none). */
function weekdayOf(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  // Date.UTC on whole days is pure arithmetic — no local-time interpretation.
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay(); // 0 = Sunday
}

/** The epoch-day number of an ISO date, so stepping days is adding integers. */
function dayNumber(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Math.floor(Date.UTC(y, m - 1, d) / 86_400_000);
}

/**
 * The gate itself: does the scanner operate on this IST calendar date?
 *
 * Weekday rule first (Sat/Sun never, unless forced open by `on_days` — a
 * Saturday save is rare enough to honor rather than argue with), then the two
 * exception lists. A date in both lists cannot reach here: `parseDays` refuses
 * the document.
 */
export function isTradingDay(doc: DaysDoc, iso: string): boolean {
  if (!isIsoDate(iso)) return false;
  const weekday = weekdayOf(iso);
  const forced = doc.on_days.some((day) => day.date === iso);
  if (forced) return true;
  if (weekday === 0 || weekday === 6) return false;
  return !doc.off_days.some((day) => day.date === iso);
}

/**
 * The next `count` dates from `from` (inclusive) the gate would let through —
 * what the page shows so the operator can eyeball the week they just edited.
 */
export function upcomingTradingDays(doc: DaysDoc, from: string, count: number): string[] {
  if (!isIsoDate(from)) return [];
  const days: string[] = [];
  const start = dayNumber(from);
  for (let ahead = 0; days.length < count && ahead < 370; ahead += 1) {
    const iso = new Date((start + ahead) * 86_400_000).toISOString().slice(0, 10);
    if (isTradingDay(doc, iso)) days.push(iso);
  }
  return days;
}

/** Field-level diff for the audit row, in the same shape the strategies journal uses. */
export function daysDiff(before: DaysDoc, after: DaysDoc): Record<string, { from: string; to: string }> {
  const show = (list: DayEntry[]) => (list.length === 0 ? "—" : list.map((day) => day.date).join(","));
  const changes: Record<string, { from: string; to: string }> = {};
  const off = { from: show(before.off_days), to: show(after.off_days) };
  if (off.from !== off.to) changes["days.off_days"] = off;
  const on = { from: show(before.on_days), to: show(after.on_days) };
  if (on.from !== on.to) changes["days.on_days"] = on;
  return changes;
}
