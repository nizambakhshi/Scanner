/**
 * The replay source: the same `ScannerSource` surface, served from the checked-in
 * JSON sample the offline replay produced
 * (`worker/worker/scripts/export_dashboard_fixture.py`).
 *
 * This exists so the dashboard can be reviewed, screenshot-tested and deployed
 * before a database is connected. It is **not** a mock: the rows are the output of
 * the real pipeline driven by scripted ticks, and every one of them keeps the
 * `is_simulated` flag the pipeline stamped on it. `meta.simulated` is `true` here,
 * and the layout turns that into a banner the operator cannot miss — mixing a
 * replayed candle into live decisions is the exact failure this project refuses.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  dateParam,
  mapCandle,
  mapConfig,
  mapCoverage,
  mapDelivery,
  mapEvent,
  mapHistory,
  mapInstrument,
  mapSignal,
  mapSnapshot,
  mapWatchlist,
  mapWorker,
  type Raw,
} from "./rows";
import type {
  InitialScanSummary,
  InstrumentDetail,
  ScannerSource,
  SignalFilter,
  SourceMeta,
} from "./types";

type Fixture = {
  meta?: Record<string, unknown>;
  coverage?: Raw;
  worker_status?: Raw | null;
  config?: Raw | null;
  initial_scan?: Raw | null;
  session?: Raw | null;
  history?: Raw[];
  instruments?: Raw[];
  watchlist?: Raw[];
  events?: Raw[];
  signals?: Raw[];
  deliveries?: Raw[];
  candles?: Record<string, Raw[]>;
  snapshots?: Record<string, Raw[]>;
};

let cache: { path: string; fixture: Fixture } | null = null;
let readError: string | null = null;

/**
 * The fixture is read from a statically-known subfolder. Spelling the segments out
 * literally (rather than joining a runtime constant) is what keeps Turbopack's
 * deployment tracing scoped to `data/` instead of walking the whole project into
 * the serverless bundle.
 */
function load(): Fixture | null {
  const path = join(process.cwd(), "data", "replay-fixture.json");
  if (cache?.path === path) return cache.fixture;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as Fixture;
    cache = { path, fixture: parsed };
    readError = null;
    return parsed;
  } catch (error) {
    readError = error instanceof Error ? error.message : String(error);
    return null;
  }
}

/** Why the sample could not be read — shown on the health page, never guessed at. */
export function fixtureError(): string | null {
  load();
  return readError;
}

const same = (value: unknown, needle: string) => String(value ?? "").toUpperCase() === needle.toUpperCase();

export const replaySource: ScannerSource = {
  name: "replay",

  async meta(): Promise<SourceMeta> {
    const fixture = load();
    const meta = (fixture?.meta ?? {}) as Record<string, unknown>;
    return {
      source: "replay",
      simulated: meta.simulated !== false,
      banner: typeof meta.banner === "string" ? meta.banner : null,
      tradingDate: typeof meta.trading_date === "string" ? meta.trading_date : null,
      note: typeof meta.note === "string" ? meta.note : null,
    };
  },

  async coverage() {
    const fixture = load();
    return fixture?.coverage ? mapCoverage(fixture.coverage) : null;
  },

  async workerStatus() {
    const fixture = load();
    return fixture?.worker_status ? [mapWorker(fixture.worker_status)] : [];
  },

  async currentWatchlist(tradingDate) {
    const fixture = load();
    const wanted = dateParam(tradingDate);
    return (fixture?.watchlist ?? [])
      .map(mapWatchlist)
      .filter((row) => wanted === null || row.trading_date === wanted)
      .sort((a, b) => {
        const tracked = Number(b.is_active) - Number(a.is_active);
        if (tracked !== 0) return tracked;
        return (b.latest_score ?? -1) - (a.latest_score ?? -1);
      });
  },

  async signals(filter: SignalFilter = {}) {
    const fixture = load();
    const wanted = dateParam(filter.tradingDate);
    const rows = (fixture?.signals ?? [])
      .map(mapSignal)
      .filter((row) => wanted === null || row.trading_date === wanted)
      .filter((row) => !filter.signalType || row.signal_type === filter.signalType)
      .filter((row) => !filter.symbol || same(row.symbol, filter.symbol))
      .sort((a, b) => (a.event_timestamp < b.event_timestamp ? 1 : -1));
    return rows.slice(0, Math.min(Math.max(filter.limit ?? 100, 1), 500));
  },

  async history(limit = 30) {
    const fixture = load();
    return (fixture?.history ?? []).map(mapHistory).slice(0, Math.min(Math.max(limit, 1), 365));
  },

  async config() {
    const fixture = load();
    return fixture?.config ? mapConfig(fixture.config) : null;
  },

  async initialScan() {
    const fixture = load();
    const raw = fixture?.initial_scan;
    if (!raw) return null;
    const completed = raw.completed_at;
    return { ...(raw as unknown as InitialScanSummary), completed_at: typeof completed === "string" ? completed : null };
  },

  async instrument(symbol: string): Promise<InstrumentDetail | null> {
    const fixture = load();
    if (!fixture) return null;
    const row = (fixture.instruments ?? []).find(
      (item) => same(item.symbol, symbol) || same(item.trading_symbol, symbol),
    );
    const instrument = row ? mapInstrument(row) : null;
    if (!instrument) return null;
    const key = instrument.symbol;
    const candles = (fixture.candles?.[key] ?? []).map(mapCandle);
    const snapshots = (fixture.snapshots?.[key] ?? []).map(mapSnapshot);
    const events = (fixture.events ?? []).map(mapEvent).filter((event) => same(event.symbol, key));
    const membership =
      (fixture.watchlist ?? []).map(mapWatchlist).find((watched) => watched.instrument_id === instrument.id) ?? null;
    // The fixture exporter predates candles_1d/stock_details; an honestly empty
    // RSI panel beats a fabricated one (same rule as the sector tile below).
    return { instrument, candles, snapshots, events, membership, overview: null, daily_rsi: [], weekly_rsi: [] };
  },

  // The fixture predates the nightly daily-bar/profile syncs and carries no
  // stock_details rows; the /stocks page shows its empty state under the
  // replay banner rather than inventing numbers (§19 — coverage is counted,
  // never implied).
  async stocksOverview() {
    return [];
  },

  async deliveries(signalIds: number[]) {
    const fixture = load();
    const wanted = new Set(signalIds.map((id) => Math.trunc(id)));
    return (fixture?.deliveries ?? []).map(mapDelivery).filter((row) => wanted.has(row.signal_id));
  },

  // The fixture exporter predates the sector tile and carries no index rows;
  // an honestly empty tile beats a fabricated one (§19 — coverage is counted,
  // never implied).
  async sectorQuotes() {
    return [];
  },
};

/** The single session row the sample describes, for pages that show "this session". */
export async function replaySession(): Promise<Raw | null> {
  const fixture = load();
  return fixture?.session ?? null;
}

export async function replayInstruments(): Promise<ReturnType<typeof mapInstrument>[]> {
  const fixture = load();
  return (fixture?.instruments ?? []).map(mapInstrument);
}
