"use client";

/**
 * The sector chip: a symbol's NIFTY sector index, coloured by that index's
 * trend — green while the index is up on the day, red while down, muted until
 * a reading exists. Unmapped symbols render a dash: not every F&O name is a
 * constituent of one of the twelve tracked sector indices, and that is an
 * honest gap, not a missing edge.
 *
 * Every chip on a page shares one module-level poller (~15 s) of
 * `/api/sectors`; the server render supplies its first reading through
 * `initial`, so the colour is already right before any client code runs. The
 * Board's tile remains the freshness authority — the chip colours by the sign
 * of the last written change and the tooltip carries that reading's own
 * timestamp, so staleness is identifiable rather than silently green.
 */

import { useEffect, useState } from "react";
import { clock, pct } from "@/lib/format";
import type { SectorRead, SectorReads } from "@/lib/sector-reads";
import { SECTOR_LABELS, SECTOR_NAMES, type SectorKey } from "@/lib/sector-map";

const POLL_MS = 15_000;

let reads: SectorReads | null = null;
let inflight = false;
const listeners = new Set<(next: SectorReads) => void>();
let timer: ReturnType<typeof setInterval> | null = null;

async function poll(): Promise<void> {
  if (inflight) return;
  inflight = true;
  try {
    const response = await fetch("/api/sectors", { cache: "no-store" });
    if (!response.ok) return; // keep the last known reading; the tile shows feed trouble
    const rows = (await response.json()) as Array<Record<string, unknown>>;
    const next: Record<string, SectorRead> = {};
    for (const row of rows ?? []) {
      const symbol = row.index_symbol;
      if (typeof symbol !== "string") continue;
      next[symbol] = {
        change_pct: typeof row.change_pct === "number" ? row.change_pct : null,
        at: typeof row.source_updated_at === "string" ? row.source_updated_at : null,
      };
    }
    reads = next;
    for (const listener of listeners) listener(next);
  } catch {
    // A transport hiccup keeps the previous reading on screen.
  } finally {
    inflight = false;
  }
}

function subscribe(listener: (next: SectorReads) => void): () => void {
  listeners.add(listener);
  if (timer === null) timer = setInterval(poll, POLL_MS);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function useSectorRead(sector: SectorKey | null, initial: SectorRead | null): SectorRead | null {
  const [read, setRead] = useState<SectorRead | null>(initial);
  useEffect(() => {
    if (!sector) return;
    const update = (next: SectorReads) => {
      const fresh = next[sector];
      if (fresh) setRead(fresh);
    };
    if (reads) update(reads);
    return subscribe(update);
  }, [sector]);
  return read;
}

/** The colour rule, pure: green above zero, red below, muted until read —
 *  exactly flat is not "up". */
export function sectorTone(change: number | null): string {
  if (change === null) return "border-line-2 bg-surface-2 text-muted";
  if (change > 0) return "border-positive/40 bg-positive/10 text-positive";
  if (change < 0) return "border-negative/40 bg-negative/10 text-negative";
  return "border-line-2 bg-surface-2 text-ink-2";
}

/** The tooltip's reading: signed percent, with the reading's own time when known. */
export function sectorReading(change: number | null, at: string | null): string {
  return change === null ? "reading not in yet" : `${pct(change)}${at ? ` at ${clock(at)} IST` : ""}`;
}

export function SectorChip({
  sector,
  initial = null,
}: {
  sector: SectorKey | null;
  initial?: SectorRead | null;
}) {
  const read = useSectorRead(sector, initial);
  if (!sector) {
    return (
      <span className="text-muted" title="Not a constituent of any tracked NIFTY sector index">
        —
      </span>
    );
  }
  const change = read?.change_pct ?? null;
  return (
    <span className={`chip ${sectorTone(change)}`} title={`${SECTOR_NAMES[sector]} · ${sectorReading(change, read?.at ?? null)}`}>
      {SECTOR_LABELS[sector]}
    </span>
  );
}
