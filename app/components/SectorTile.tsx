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
 * STALE_MS is dimmed rather than hidden: an empty chip would be
 * indistinguishable from a chip that was never fetched.
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
    return () => {
      cancelled = true;
      if (timer.current) clearInterval(timer.current);
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
          <>Live from the broker feed · refreshed every 5 s · a dimmed chip means the quote is stale</>
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
      className={`rounded border px-2.5 py-2 ${stale ? "border-line bg-surface-2 opacity-60" : "border-line"}`}
      title={
        row.source_updated_at
          ? `${row.display_name} · quote at ${row.source_updated_at}`
          : `${row.display_name} · no quote flushed yet`
      }
    >
      <p className="truncate text-[11px] uppercase tracking-wide text-muted">{row.display_name}</p>
      {row.last_price === null ? (
        <p className="num mt-0.5 text-[13px] text-muted">waiting…</p>
      ) : (
        <div className="mt-0.5 flex items-baseline justify-between gap-2">
          <span className="num text-[13px] font-semibold">{row.last_price.toFixed(2)}</span>
          <span
            className={`num text-[12px] ${row.change_pct === null ? "text-muted" : up ? "text-positive" : "text-negative"}`}
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
