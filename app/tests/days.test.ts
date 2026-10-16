import assert from "node:assert/strict";
import { test } from "node:test";
import {
  daysDiff,
  defaultDays,
  isIsoDate,
  isTradingDay,
  parseDays,
  upcomingTradingDays,
  type DayEntry,
} from "../lib/days";

/**
 * The gate the /days page edits and the worker obeys, tested as pure arithmetic:
 * no database, no clock, every date spelled out.
 */

const doc = (overrides: { off_days?: DayEntry[]; on_days?: DayEntry[] } = {}) => ({
  version: 1 as const,
  off_days: overrides.off_days ?? [],
  on_days: overrides.on_days ?? [],
});

test("the weekday rule: Mon–Fri on, Sat–Sun off", () => {
  const plain = defaultDays();
  assert.equal(isTradingDay(plain, "2026-10-19"), true, "Monday");
  assert.equal(isTradingDay(plain, "2026-10-23"), true, "Friday");
  assert.equal(isTradingDay(plain, "2026-10-24"), false, "Saturday");
  assert.equal(isTradingDay(plain, "2026-10-25"), false, "Sunday");
  assert.equal(isTradingDay(plain, "not-a-date"), false);
});

test("a skip takes a weekday off, and a force-open takes a weekend on", () => {
  const d = doc({ off_days: [{ date: "2026-10-26", note: "Diwali" }], on_days: [{ date: "2026-10-24", note: null }] });
  assert.equal(isTradingDay(d, "2026-10-26"), false, "Monday marked off");
  assert.equal(isTradingDay(d, "2026-10-27"), true, "the next Monday is untouched");
  assert.equal(isTradingDay(d, "2026-10-24"), true, "Saturday forced open");
  assert.equal(isTradingDay(d, "2026-10-25"), false, "the Sunday beside it still rests");
});

test("a date spelled wrong never opens a session", () => {
  assert.equal(isIsoDate("2026-02-30"), false, "February has no 30th");
  assert.equal(isIsoDate("2026-13-01"), false, "month 13 does not exist");
  assert.equal(isIsoDate("2024-02-29"), true, "2024 is a leap year");
  assert.equal(isIsoDate("2026-02-29"), false, "2026 is not");
  assert.equal(isIsoDate("26-02-01"), false, "four-digit year or nothing");
  assert.equal(isTradingDay(defaultDays(), "2026-02-30"), false);
});

test("parsing sorts, dedupes, and refuses a self-contradicting calendar", () => {
  const parsed = parseDays({
    off_days: [
      { date: "2026-12-25", note: "  " },
      { date: "2026-10-26", note: "Diwali" },
      { date: "2026-10-26", note: "duplicate" },
    ],
    on_days: [],
  });
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.deepEqual(parsed.doc.off_days.map((day) => day.date), ["2026-10-26", "2026-12-25"], "sorted");
  assert.equal(parsed.doc.off_days.length, 2, "the duplicate folded into the first row");
  assert.equal(parsed.doc.off_days[0]?.note, "Diwali");
  assert.equal(parsed.doc.off_days[1]?.note, null, "a blank note is no note");

  const contradiction = parseDays({
    off_days: [{ date: "2026-10-26", note: null }],
    on_days: [{ date: "2026-10-26", note: "special" }],
  });
  assert.equal(contradiction.ok, false);
  assert.match(contradiction.ok ? "" : contradiction.error, /both/);

  const malformed = parseDays({ off_days: [{ date: "2026-13-01", note: null }], on_days: [] });
  assert.equal(malformed.ok, false);
  assert.match(malformed.ok ? "" : malformed.error, /YYYY-MM-DD/);

  assert.equal(parseDays(null).ok, true, "no document is the plain weekday rule");
  assert.equal(parseDays({ off_days: "Monday" }).ok, false);
});

test("the preview lists the days the gate would let through", () => {
  // Mon 19 → Fri 23, then Mon 26 off (Diwali), back on Tue 27.
  const d = doc({ off_days: [{ date: "2026-10-26", note: "Diwali" }] });
  assert.deepEqual(upcomingTradingDays(d, "2026-10-19", 4), ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]);
  assert.deepEqual(upcomingTradingDays(d, "2026-10-23", 3), ["2026-10-23", "2026-10-27", "2026-10-28"]);
  assert.deepEqual(upcomingTradingDays(defaultDays(), "2026-10-24", 2), ["2026-10-26", "2026-10-27"], "from a Saturday: the skipped weekend lands on Monday");
  assert.deepEqual(
    upcomingTradingDays(doc({ on_days: [{ date: "2026-10-24", note: null }] }), "2026-10-24", 2),
    ["2026-10-24", "2026-10-26"],
    "the forced Saturday counts, the plain Sunday does not",
  );
  assert.deepEqual(upcomingTradingDays(defaultDays(), "garbage", 3), []);
});

test("the audit diff names the dates that moved", () => {
  const before = doc({ off_days: [{ date: "2026-10-26", note: null }] });
  const after = doc({ off_days: [{ date: "2026-10-26", note: null }], on_days: [{ date: "2026-11-14", note: null }] });
  const changes = daysDiff(before, after);
  assert.deepEqual(Object.keys(changes), ["days.on_days"]);
  assert.equal(changes["days.on_days"].from, "—");
  assert.equal(changes["days.on_days"].to, "2026-11-14");
  assert.deepEqual(daysDiff(before, before), {}, "an unchanged save writes nothing");
});
