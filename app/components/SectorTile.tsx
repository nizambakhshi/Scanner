"use client";

import { useEffect, useRef, useState } from "react";
import type { SectorQuoteRow } from "@/lib/types";

/**
 * The Nifty sector tile: thirteen index chips refreshed straight from
 * `/api/sectors`, which reads the worker's `sector_quotes` scratchpad.
 *
 * Why client polling and not the server render: the numbers the operator acts
 * on are at most one candle behind the tape, but sectors are context, not
 * signals — a 5-second poll over a 13-row JSON endpoint is cheap and keeps the
 * board's server render independent of this tile's health. A quote older than
 * STALE_MS keeps its border and loses its liveness — the edge goes dashed and the
 * figure goes quiet — rather than vanishing or fading to an invisible outline: an
 * empty chip would be indistinguishable from a chip that was never fetched, and a
 * board at 08:00 is entirely stale quotes until the open, which is exactly when the
 * operator reads it.
 */

const POLL_MS = 5_000;
/** The worker flushes every 5 s; beyond this the feed or flush loop is down. */
const STALE_MS = 30_000;

export function SectorTile({ initial }: { initial: SectorQuoteRow[] | null }) {
  const [rows, setRows] = useState<SectorQuoteRow[] | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      if (document.hidden) return; // see the visibility listener below
      try {
        const response = await fetch("/api/sectors", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = (await response.json()) as SectorQuoteRow[];
        if (!cancelled) {
          setRows(payload);
          setError(null);
          setFetchedAt(Date.now());
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void poll();
    timer.current = setInterval(poll, POLL_MS);
    // A hidden tab that keeps polling keeps one Vercel instance warm forever,
    // and that instance holds a client slot in the 15-slot session pooler —
    // background tabs are how the pool fills up. On return to the foreground,
    // poll at once so the operator does not look at last-minute numbers.
    function onVisible() {
      if (!document.hidden) void poll();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (timer.current) clearInterval(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // Staleness is evaluated at render time; every poll re-renders, so chips
  // cross the threshold within one poll of going quiet.
  const now = fetchedAt ?? Date.now();

  return (
    <div className="px-4 py-3">
      {rows === null && !error ? (
        <p className="text-[12px] text-muted">Loading sector quotes…</p>
      ) : rows && rows.length === 0 ? (
        <p className="text-[12px] text-muted">
          No sector indices configured. Run <span className="num">scanner-sync-instruments</span> to
          populate the index list.
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
          {(rows ?? []).map((row) => (
            <SectorChip key={row.index_symbol} row={row} now={now} />
          ))}
        </div>
      )}
      <p className="mt-2 text-[11px] text-muted">
        {error ? (
          <span className="text-amber">
            Last refresh failed ({error}) — showing the previous values.
          </span>
        ) : (
          <>Live from the broker feed · refreshed every 5 s · a dashed chip means the quote is stale</>
        )}
      </p>
    </div>
  );
}

function SectorChip({ row, now }: { row: SectorQuoteRow; now: number }) {
  const stale =
    row.source_updated_at === null || now - Date.parse(row.source_updated_at) > STALE_MS;
  const up = (row.change_pct ?? 0) >= 0;
  return (
    <div
      className={`rounded-md border px-2.5 py-2 transition-colors ${
        stale
          ? "border-dashed border-line-2 bg-surface-2 text-muted"
          : "border-line-2 bg-surface hover:border-ink-2/60"
      }`}
      title={
        row.source_updated_at
          ? `${row.display_name} · quote at ${row.source_updated_at}`
          : `${row.display_name} · no quote flushed yet`
      }
    >
      <p className="truncate text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">
        {row.display_name}
      </p>
      {row.last_price === null ? (
        <p className="num mt-1 text-[13px] text-muted">waiting…</p>
      ) : (
        <div className="mt-1 flex items-baseline justify-between gap-1.5">
          <span className="num text-[13px] font-medium">{row.last_price.toFixed(2)}</span>
          {/* The number that says which way the sector moved is the one worth reading
              at a glance, so it gets the heaviest cut of the face that carries every
              figure here, plus a tint of its own direction. */}
          <span
            className={`num rounded px-1 py-px text-[13px] font-bold ${
              row.change_pct === null
                ? "text-muted"
                : up
                  ? "bg-positive/12 text-positive"
                  : "bg-negative/12 text-negative"
            }`}
          >
            {row.change_pct === null
              ? "—"
              : `${up ? "+" : ""}${row.change_pct.toFixed(2)}%`}
          </span>
        </div>
      )}
    </div>
  );
}
