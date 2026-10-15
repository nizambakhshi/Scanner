import { test } from "node:test";
import assert from "node:assert/strict";
import { SECTOR_LABELS, sectorFor } from "../lib/sector-map";
import { sectorReadsFrom } from "../lib/sector-reads";
import { sectorReading, sectorTone } from "../components/SectorChip";
import type { SectorQuoteRow } from "../lib/types";

/**
 * The sector chip's rules: which index a symbol maps to (most specific wins),
 * and the colour-by-trend contract — green up, red down, muted until read.
 */

test("the chip rule: green up, red down, muted until read", () => {
  assert.match(sectorTone(0.42), /text-positive/);
  assert.match(sectorTone(-0.42), /text-negative/);
  assert.match(sectorTone(null), /text-muted/);
  // Exactly flat is not "up".
  assert.match(sectorTone(0), /text-ink-2/);
  assert.ok(!sectorTone(0).includes("text-positive"));
});

test("the reading spells the sign, and the time when it is known", () => {
  assert.equal(sectorReading(0.5, "2026-10-12T11:03:00+05:30"), "+0.50% at 11:03 IST");
  assert.equal(sectorReading(-0.25, null), "-0.25%");
  assert.equal(sectorReading(null, "2026-10-12T11:03:00+05:30"), "reading not in yet");
});

test("sector lookup is case- and space-tolerant, unknown is null", () => {
  assert.equal(sectorFor(" sbin "), "NIFTY PSU BANK");
  assert.equal(sectorFor("reliance"), "NIFTY ENERGY");
  assert.equal(sectorFor("SIMBREAK"), null);
  assert.equal(sectorFor(null), null);
  assert.equal(sectorFor(""), null);
});

test("the most specific index wins when a symbol sits in several lists", () => {
  // These banks are in both the sector bank lists and FINNIFTY/BANKNIFTY; the
  // chip must say the specific one, not the broad one.
  assert.equal(sectorFor("SBIN"), "NIFTY PSU BANK"); // also in BANKNIFTY + FINNIFTY
  assert.equal(sectorFor("ICICIBANK"), "NIFTY PVT BANK"); // also in BANKNIFTY + FINNIFTY
  assert.equal(sectorFor("BAJFINANCE"), "FINNIFTY"); // in FINNIFTY only
  assert.equal(SECTOR_LABELS[sectorFor("HDFCBANK") ?? "BANKNIFTY"], "PVT BANK");
});

test("the first-paint fold keeps the reading's own timestamp and never invents one", () => {
  const rows: SectorQuoteRow[] = [
    {
      index_symbol: "NIFTY IT",
      display_name: "Nifty IT",
      display_order: 3,
      last_price: 38_000,
      previous_close: 37_800,
      change_pct: 0.53,
      source_updated_at: "2026-10-15T11:03:00+05:30",
    },
    {
      // A row whose quote has not been flushed yet: the chip colours muted and
      // the tooltip says "reading not in yet" — not "flat".
      index_symbol: "NIFTY PHARMA",
      display_name: "Nifty Pharma",
      display_order: 5,
      last_price: null,
      previous_close: null,
      change_pct: null,
      source_updated_at: null,
    },
  ];
  const reads = sectorReadsFrom(rows);
  assert.deepEqual(reads["NIFTY IT"], { change_pct: 0.53, at: "2026-10-15T11:03:00+05:30" });
  assert.deepEqual(reads["NIFTY PHARMA"], { change_pct: null, at: null });
  assert.equal(sectorReadsFrom([])["NIFTY IT"], undefined);
});
