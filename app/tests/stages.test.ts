import assert from "node:assert/strict";
import { test } from "node:test";
import { buildStages, removedCount, trackedCount, weakeningCount } from "@/lib/stages";
import type {
  CoverageRow,
  HistoryRow,
  InitialScanSummary,
  SignalRow,
  WatchlistRow,
  WatchlistState,
} from "@/lib/types";

/** Fixtures carry only what the stage arithmetic reads. */
function member(state: WatchlistState, symbol = "X"): WatchlistRow {
  return { symbol, current_state: state } as WatchlistRow;
}

function coverage(overrides: Partial<CoverageRow>): CoverageRow {
  return {
    nifty500_count: 6,
    mapped_count: 6,
    unmapped_count: 0,
    ambiguous_count: 0,
    invalid_count: 0,
    active_universe_count: 6,
    fno_count: 0,
    last_verified_at: null,
    ...overrides,
  };
}

function scan(overrides: Partial<InitialScanSummary>): InitialScanSummary {
  return {
    universe: 6,
    counts: { evaluated: 5, qualified: 2 },
    ranked: ["SIMREENTRY", "SIMFADE"],
    candles_required: 5,
    completed_at: "2026-10-12T09:39:20+05:30",
    ...overrides,
  };
}

function session(overrides: Partial<HistoryRow>): HistoryRow {
  return {
    trading_session_id: 1,
    trading_date: "2026-10-12",
    session_status: "FINALIZED",
    session_open_at: "2026-10-12T09:15:00+05:30",
    session_close_at: "2026-10-12T15:30:00+05:30",
    initial_scan_completed_at: null,
    finalized_at: null,
    stale_data_seconds: 0,
    universe_count: 6,
    mapped_count: 6,
    subscribed_count: 6,
    monitored_count: 5,
    qualified_count: 3,
    signals_generated: 15,
    telegram_delivered: 3,
    telegram_failed: 1,
    notes: null,
    ...overrides,
  };
}

function signal(delivered: boolean, failed: boolean): SignalRow {
  return { telegram_delivered: delivered, telegram_failed: failed } as SignalRow;
}

const MEMBERS = [
  member("ACTIVE", "ACT"),
  member("REQUALIFIED", "REQ"),
  member("WEAKENING", "WEAK"),
  member("DATA_STALE", "FROZEN"),
  member("REMOVED", "GONE"),
  member("WARMING_UP", "WARM"),
];

test("the sequence is exactly six stages, numbered in order", () => {
  const stages = buildStages({
    coverage: coverage({}),
    scan: scan({}),
    members: MEMBERS,
    session: session({}),
    signals: [],
  });
  assert.deepEqual(
    stages.map((stage) => stage.n),
    [1, 2, 3, 4, 5, 6],
  );
  assert.deepEqual(
    stages.map((stage) => stage.label),
    ["F&O universe", "Stock discovery", "Initial watchlist", "Alerts", "Continuous qualification", "Removed"],
  );
});

test("stage 1 counts the F&O flag when the database has it", () => {
  const stages = buildStages({
    coverage: coverage({ fno_count: 212, mapped_count: 494, nifty500_count: 494 }),
    scan: null,
    members: [],
    session: null,
    signals: [],
  });
  assert.equal(stages[0].value, "212");
});

test("stage 1 falls back to the NIFTY500 count only when the F&O flag is absent, and says so", () => {
  const stages = buildStages({ coverage: coverage({ fno_count: 0 }), scan: null, members: [], session: null, signals: [] });
  assert.equal(stages[0].value, "6");
  assert.match(stages[0].hint, /no F&O flag/);

  const unread = buildStages({ coverage: null, scan: null, members: [], session: null, signals: [] });
  assert.equal(unread[0].value, "—", "an unread coverage is a dash, never a zero");
});

test("stage 2 is the ranked set, and a skipped scan is a dash with the reason", () => {
  const ranked = buildStages({ coverage: null, scan: scan({}), members: [], session: null, signals: [] });
  assert.equal(ranked[1].value, "2");
  assert.match(ranked[1].hint, /5 evaluated/);

  const skipped = buildStages({
    coverage: null,
    scan: scan({ skipped: "outside window", ranked: undefined }),
    members: [],
    session: null,
    signals: [],
  });
  assert.equal(skipped[1].value, "—");
  assert.match(skipped[1].hint, /outside window/);

  const none = buildStages({ coverage: null, scan: null, members: [], session: null, signals: [] });
  assert.equal(none[1].value, "—");
});

test("stage 3 counts every membership of the day — removals included, the table keeps them too", () => {
  const stages = buildStages({ coverage: null, scan: null, members: MEMBERS, session: null, signals: [] });
  assert.equal(stages[2].value, "6");
});

test("stage 4 takes the day's count from the session summary, delivery split included", () => {
  const stages = buildStages({ coverage: null, scan: null, members: [], session: session({}), signals: [] });
  assert.equal(stages[3].value, "15");
  assert.match(stages[3].hint, /3 delivered · 1 failed/);
  assert.equal(stages[3].tone, "negative", "a failed alert must colour the stage");
});

test("stage 4 falls back to the feed rows when no session summary exists — an honest floor", () => {
  const stages = buildStages({
    coverage: null,
    scan: null,
    members: [],
    session: null,
    signals: [signal(true, false), signal(true, false), signal(false, false)],
  });
  assert.equal(stages[3].value, "3");
  assert.match(stages[3].hint, /2 delivered · 0 failed/);
});

test("stage 5 tracks ACTIVE and REQUALIFIED only; stage 6 is the removed set", () => {
  const stages = buildStages({ coverage: null, scan: null, members: MEMBERS, session: null, signals: [] });
  assert.equal(stages[4].value, "2");
  assert.match(stages[4].hint, /2 weakening/);
  assert.equal(stages[5].value, "1");
});

test("the counters agree with the filters the chips build on", () => {
  assert.equal(trackedCount(MEMBERS), 2);
  assert.equal(weakeningCount(MEMBERS), 2);
  assert.equal(removedCount(MEMBERS), 1);
  // WARMING_UP is in none of the three cuts — it has not produced a verdict yet.
  assert.equal(trackedCount(MEMBERS) + weakeningCount(MEMBERS) + removedCount(MEMBERS), MEMBERS.length - 1);
});
