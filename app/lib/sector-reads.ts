/**
 * Shaping the sector indices' rows for first paint.
 *
 * The stock tables take a `SectorReads` map (index_symbol → the reading its
 * chips colour by) so a chip is already the right colour in the server-rendered
 * HTML; the pages build that map from the source's `sectorQuotes()` rows with
 * `sectorReadsFrom`. Server-safe — no hooks — which is why it lives here rather
 * than beside the chip component.
 */
import type { SectorQuoteRow } from "./types";

export type SectorRead = { change_pct: number | null; at: string | null };
/** One reading per index_symbol, exactly the rows `/api/sectors` returns. */
export type SectorReads = Readonly<Record<string, SectorRead>>;

export function sectorReadsFrom(rows: readonly SectorQuoteRow[]): SectorReads {
  const out: Record<string, SectorRead> = {};
  for (const row of rows) out[row.index_symbol] = { change_pct: row.change_pct, at: row.source_updated_at };
  return out;
}
