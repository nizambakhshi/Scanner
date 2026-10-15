#!/usr/bin/env node
/**
 * Generate `lib/sector-map.ts` — symbol → NIFTY sector index — from NSE's
 * published constituent CSVs.
 *
 *   node app/scripts/gen-sector-map.mjs [/tmp/nse-sectors]
 *
 * The CSVs are not committed (NSE reshuffles constituents semi-annually, so
 * the map is regenerated rather than patched by hand). Fetch them first:
 *
 *   mkdir -p /tmp/nse-sectors && cd /tmp/nse-sectors
 *   for f in ind_niftyitlist ind_niftyautolist ind_niftypharmalist \
 *            ind_niftyfmcglist ind_niftymetallist ind_niftyenergylist \
 *            ind_niftyrealtylist ind_niftymedialist ind_niftypsubanklist \
 *            ind_nifty_privatebanklist ind_niftyfinancelist ind_niftybanklist; do
 *     curl -fsSL "https://nsearchives.nseindia.com/content/indices/${f}.csv" -o "${f}.csv"
 *   done
 *
 * Each CSV is `Company Name,Industry,Symbol,Series,ISIN Code`. The emitted
 * `index_symbol` strings are exactly the ones in `worker/.../sectors.py` —
 * the chip joins on them, so a typo here is a chip that never colours.
 *
 * Precedence, most specific first: PSU BANK, PVT BANK, IT, AUTO, PHARMA, FMCG,
 * METAL, ENERGY, REALTY, MEDIA, FIN SERVICES, BANK. A PSU bank sits in both
 * the bank and the PSU-bank list; the chip should say the specific one.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CSV_DIR = process.argv[2] ?? "/tmp/nse-sectors";

/** [csv basename, index_symbol in precedence order]. */
const LISTS = [
  ["ind_niftypsubanklist", "NIFTY PSU BANK"],
  ["ind_nifty_privatebanklist", "NIFTY PVT BANK"],
  ["ind_niftyitlist", "NIFTY IT"],
  ["ind_niftyautolist", "NIFTY AUTO"],
  ["ind_niftypharmalist", "NIFTY PHARMA"],
  ["ind_niftyfmcglist", "NIFTY FMCG"],
  ["ind_niftymetallist", "NIFTY METAL"],
  ["ind_niftyenergylist", "NIFTY ENERGY"],
  ["ind_niftyrealtylist", "NIFTY REALTY"],
  ["ind_niftymedialist", "NIFTY MEDIA"],
  ["ind_niftyfinancelist", "FINNIFTY"],
  ["ind_niftybanklist", "BANKNIFTY"],
];

const LABELS = {
  "NIFTY PSU BANK": "PSU BANK",
  "NIFTY PVT BANK": "PVT BANK",
  "NIFTY IT": "IT",
  "NIFTY AUTO": "AUTO",
  "NIFTY PHARMA": "PHARMA",
  "NIFTY FMCG": "FMCG",
  "NIFTY METAL": "METAL",
  "NIFTY ENERGY": "ENERGY",
  "NIFTY REALTY": "REALTY",
  "NIFTY MEDIA": "MEDIA",
  FINNIFTY: "FIN SVC",
  BANKNIFTY: "BANK",
};

/** Minimal RFC-4180-ish split for one line (quoted commas survive). */
function splitCsvLine(line) {
  const cells = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      cells.push(cell);
      cell = "";
    } else {
      cell += ch;
    }
  }
  cells.push(cell);
  return cells.map((value) => value.trim());
}

const bySymbol = new Map(); // symbol -> index_symbol; first (most specific) list wins
const perList = new Map();
for (const [file, indexSymbol] of LISTS) {
  const text = readFileSync(join(CSV_DIR, `${file}.csv`), "utf8").replace(/^\uFEFF/, "");
  const lines = text.split(/\r?\n/).filter((line) => line.trim().length > 0);
  const header = splitCsvLine(lines[0]).map((cell) => cell.toUpperCase());
  const symbolCol = header.indexOf("SYMBOL");
  if (symbolCol === -1) throw new Error(`${file}.csv: no Symbol column (header: ${lines[0]})`);
  let count = 0;
  for (const line of lines.slice(1)) {
    const symbol = splitCsvLine(line)[symbolCol]?.toUpperCase();
    if (!symbol) continue;
    count += 1;
    if (!bySymbol.has(symbol)) bySymbol.set(symbol, indexSymbol);
  }
  perList.set(indexSymbol, count);
  console.log(`${indexSymbol.padEnd(15)} <- ${file}.csv (${count} constituents)`);
}

const symbols = [...bySymbol.keys()].sort();
// Keys are JSON-quoted: NSE symbols include `M&M`, `L&TFH` and `360ONE`, none
// of which is a bare JS identifier.
const rows = symbols
  .map((symbol) => `  ${JSON.stringify(symbol)}: ${JSON.stringify(bySymbol.get(symbol))},`)
  .join("\n");

/** Quote a key only when it is not a bare identifier (FINNIFTY is, "NIFTY IT" is not). */
const key = (name) => (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : JSON.stringify(name));
const labelRows = Object.entries(LABELS)
  .map(([name, label]) => `  ${key(name)}: ${JSON.stringify(label)},`)
  .join("\n");

const out = `/**
 * AUTO-GENERATED — do not edit by hand. Regenerate with:
 *
 *   node app/scripts/gen-sector-map.mjs
 *
 * Sources: NSE index constituent CSVs fetched 2026-10-15 (ind_niftyitlist,
 * autolist, pharmalist, fmcglist, metallist, energylist, realtylist, medialist,
 * psubanklist, privatebanklist, financelist, banklist). NSE reshuffles these
 * semi-annually — re-fetch and regenerate then.
 *
 * Precedence, most specific first: PSU BANK, PVT BANK, IT, AUTO, PHARMA, FMCG,
 * METAL, ENERGY, REALTY, MEDIA, FIN SERVICES, BANK. A PSU bank is also a bank;
 * the chip says the specific one. The NIFTY 50 is not a sector and is therefore
 * not represented — a symbol outside all twelve lists has no chip and that is
 * an honest gap, not a missing edge.
 *
 * Keys are exactly the index_symbol strings the worker writes to
 * sector_indices (worker/worker/marketdata/sectors.py); the chip joins on
 * them, so they must not be renamed here.
 */

export type SectorKey =
  | "NIFTY PSU BANK"
  | "NIFTY PVT BANK"
  | "NIFTY IT"
  | "NIFTY AUTO"
  | "NIFTY PHARMA"
  | "NIFTY FMCG"
  | "NIFTY METAL"
  | "NIFTY ENERGY"
  | "NIFTY REALTY"
  | "NIFTY MEDIA"
  | "FINNIFTY"
  | "BANKNIFTY";

/** Short label for the chip; the full index display name goes in its tooltip. */
export const SECTOR_LABELS: Record<SectorKey, string> = {
${labelRows}
};

/** Long names as the sector tile renders them (display_name in sectors.py). */
export const SECTOR_NAMES: Record<SectorKey, string> = {
  "NIFTY PSU BANK": "Nifty PSU Bank",
  "NIFTY PVT BANK": "Nifty Private Bank",
  "NIFTY IT": "Nifty IT",
  "NIFTY AUTO": "Nifty Auto",
  "NIFTY PHARMA": "Nifty Pharma",
  "NIFTY FMCG": "Nifty FMCG",
  "NIFTY METAL": "Nifty Metal",
  "NIFTY ENERGY": "Nifty Energy",
  "NIFTY REALTY": "Nifty Realty",
  "NIFTY MEDIA": "Nifty Media",
  FINNIFTY: "Nifty Fin Service",
  BANKNIFTY: "Nifty Bank",
};

export const SECTOR_FOR_SYMBOL: Record<string, SectorKey> = {
${rows}
};

/** The sector index a symbol belongs to, or null — unknown is a fact, not a guess. */
export function sectorFor(symbol: string | null | undefined): SectorKey | null {
  if (!symbol) return null;
  return SECTOR_FOR_SYMBOL[symbol.trim().toUpperCase()] ?? null;
}
`;

const target = join(dirname(fileURLToPath(import.meta.url)), "..", "lib", "sector-map.ts");
writeFileSync(target, out);
console.log(`\n${symbols.length} symbols written to ${target}`);
