/**
 * The six-stage sequence the Watchlist page is built around.
 *
 * One trading day is a pipeline, and the operator has asked to see it as one:
 * the F&O universe is chosen → the initial scan discovers what qualifies →
 * memberships are created → alerts go out → qualification is continuously
 * re-checked → symbols that stop qualifying are removed. Every stage has a live
 * count, and every count comes from a read the page already performs; nothing
 * here queries.
 *
 * The builder is deliberately a pure function of typed rows so the arithmetic —
 * which is where wrong numbers live — is unit-testable without a database. The
 * component only paints what this returns.
 */

import { clock } from "./format";
import type { CoverageRow, HistoryRow, InitialScanSummary, SignalRow, WatchlistRow } from "./types";

export type StageTone = "neutral" | "positive" | "negative" | "amber";

export type Stage = {
  /** 1-based position; the sequence order is the information. */
  n: number;
  label: string;
  /** Digits or "—". Never a placeholder number. */
  value: string;
  hint: string;
  tone: StageTone;
  /** Where this stage's detail lives — an anchor on the page, or the Board. */
  href: string;
};

export type StageInput = {
  coverage: CoverageRow | null;
  scan: InitialScanSummary | null;
  /** Today's memberships only (the caller has already cut to the session). */
  members: WatchlistRow[];
  /** Today's session summary; null before v_session_history has a row. */
  session: HistoryRow | null;
  /** Today's feed rows — may be capped by the feed limit. */
  signals: SignalRow[];
};

const isTracked = (row: WatchlistRow) => row.current_state === "ACTIVE" || row.current_state === "REQUALIFIED";
const isWeakening = (row: WatchlistRow) => row.current_state === "WEAKENING" || row.current_state === "DATA_STALE";

export function trackedCount(members: WatchlistRow[]): number {
  return members.filter(isTracked).length;
}

export function weakeningCount(members: WatchlistRow[]): number {
  return members.filter(isWeakening).length;
}

export function removedCount(members: WatchlistRow[]): number {
  return members.filter((row) => row.current_state === "REMOVED").length;
}

export function buildStages({ coverage, scan, members, session, signals }: StageInput): Stage[] {
  const tracked = trackedCount(members);
  const weakening = weakeningCount(members);
  const removed = removedCount(members);

  // 1 ── FNO universe ---------------------------------------------------------
  // The count the worker reads as its scan set is `fno_count`. It arrives as a
  // number — 0 when no instrument carries the flag (the checked-in replay sample
  // predates the column) or when the database is empty. A non-zero flag count
  // is the scan set; a zero flag count over a populated NIFTY500 table means
  // the flag is missing, and the NIFTY500 set is what this database actually
  // holds — the hint says which set the number describes, rather than passing
  // one off as the other.
  const universeValue = coverage ? (coverage.fno_count > 0 ? coverage.fno_count : coverage.nifty500_count) : null;
  const universeHint = coverage
    ? coverage.fno_count > 0
      ? "flagged F&O — the scan set"
      : coverage.nifty500_count > 0
        ? `${coverage.nifty500_count} NIFTY500 rows · no F&O flag on this database`
        : "nothing flagged yet"
    : "coverage unread";

  // 2 ── Stock discovery ------------------------------------------------------
  // The initial scan's ranked set is the day's first judgement; when the scan
  // was skipped or never journaled, the stage is a dash, not a zero.
  const ranked = scan ? scan.ranked?.length ?? scan.counts?.qualified ?? 0 : null;
  const discoveryValue = scan && !scan.skipped ? ranked : null;
  const discoveryHint = !scan
    ? "no initial scan journaled"
    : scan.skipped
      ? `skipped: ${scan.skipped}`
      : `${scan.counts?.evaluated ?? "—"} evaluated at open${scan.completed_at ? ` · ${clock(scan.completed_at)}` : ""}`;

  // 3 ── Initial watchlist ----------------------------------------------------
  // Every membership row today is a symbol that qualified; removals stay in the
  // table, which is why this count does not shrink as the day goes on.
  // 4 ── Alerts ---------------------------------------------------------------
  // The session summary counts every signal of the day (the feed read below may
  // be capped); before a summary exists, the fetched rows are the honest floor.
  const delivered = session
    ? session.telegram_delivered
    : signals.filter((row) => row.telegram_delivered).length;
  const failed = session ? session.telegram_failed : signals.filter((row) => row.telegram_failed).length;
  const alertValue = session ? session.signals_generated : signals.length;
  const alertTone: StageTone = failed > 0 ? "negative" : "neutral";

  return [
    {
      n: 1,
      label: "F&O universe",
      value: universeValue === null ? "—" : String(universeValue),
      hint: universeHint,
      tone: "neutral",
      href: "#universe",
    },
    {
      n: 2,
      label: "Stock discovery",
      value: discoveryValue === null ? "—" : String(discoveryValue),
      hint: discoveryHint,
      tone: "neutral",
      href: "/",
    },
    {
      n: 3,
      label: "Initial watchlist",
      value: String(members.length),
      hint: "symbols that qualified today",
      tone: members.length > 0 ? "positive" : "neutral",
      href: "#members",
    },
    {
      n: 4,
      label: "Alerts",
      value: String(alertValue),
      hint: `${delivered} delivered · ${failed} failed`,
      tone: alertTone,
      href: "#alerts",
    },
    {
      n: 5,
      label: "Continuous qualification",
      value: String(tracked),
      hint: `${weakening} weakening · re-checked every candle`,
      tone: tracked > 0 ? "positive" : "neutral",
      href: "#members",
    },
    {
      n: 6,
      label: "Removed",
      value: String(removed),
      hint: "left the list — reasons kept below",
      tone: removed > 0 ? "amber" : "neutral",
      href: "#members",
    },
  ];
}
