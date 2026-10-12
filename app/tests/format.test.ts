import assert from "node:assert/strict";
import { test } from "node:test";
import { ago, bool, clock, crores, dateTime, duration, humanise, int, isValidIst, longDate, minutesSince, multiple, num, orDash, pct, price, rowKey, rsi, score, shares, strings, toIst } from "@/lib/format";

test("toIst converts an instant to Asia/Kolkata without touching the caller's timezone", () => {
  // 04:00 UTC is 09:30 IST — the session open, expressed two ways.
  assert.equal(toIst("2026-10-12T04:00:00Z"), "2026-10-12T09:30:00+05:30");
  assert.equal(toIst(new Date("2026-10-12T04:00:00Z")), "2026-10-12T09:30:00+05:30");
  assert.ok(isValidIst(toIst("2026-10-12T04:00:00Z")));
});

test("toIst refuses to invent a time", () => {
  assert.equal(toIst(null), null);
  assert.equal(toIst(undefined), null);
  assert.equal(toIst(""), null);
  assert.equal(toIst("not a date"), null);
  // 1_760_000_000_000 ms is 08:53:20 UTC, which is 14:23:20 in Kolkata.
  assert.equal(toIst(1760000000000), "2025-10-09T14:23:20+05:30");
});

test("midnight is 00, not 24", () => {
  // Some ICU versions print the 24th hour; an ISO string may not.
  assert.equal(toIst("2026-10-11T18:30:00Z"), "2026-10-12T00:00:00+05:30");
});

test("clock, dateTime and longDate read the IST string this module produces", () => {
  const ist = toIst("2026-10-12T04:00:00Z")!;
  assert.equal(clock(ist), "09:30");
  assert.equal(dateTime(ist), "12 Oct 2026 · 09:30");
  assert.equal(longDate("2026-10-12"), "Mon 12 Oct 2026");
  assert.equal(clock(null), "—");
  assert.equal(dateTime(undefined), "—");
  assert.equal(longDate(""), "—");
});

test("duration is the staleness figure an operator can compare at a glance", () => {
  assert.equal(duration(0), "0s");
  assert.equal(duration(90), "1m 30s");
  assert.equal(duration(3720), "1h 02m");
  assert.equal(duration(-5), "0s");
  assert.equal(duration(null), "—");
});

test("ago() never claims freshness it has not measured", () => {
  const now = Date.parse("2026-10-12T10:00:00+05:30");
  assert.equal(ago(toIst(now - 10_000), now), "just now");
  assert.equal(ago(toIst(now - 5 * 60_000), now), "5 min ago");
  assert.equal(ago(toIst(now - 3 * 3_600_000), now), "3 h ago");
  assert.equal(ago(null, now), "never");
  assert.equal(minutesSince(null, now), null);
});

test("numbers arrive as strings over the Postgres wire and leave as numbers", () => {
  assert.equal(num("1.5000"), 1.5);
  assert.equal(num(2), 2);
  assert.equal(num(""), null);
  assert.equal(num(null), null);
  assert.equal(num("NaN"), null);
  assert.equal(int("42"), 42);
  assert.equal(int(null, 7), 7);
  assert.equal(bool("t"), true);
  assert.equal(bool("false"), false);
  assert.equal(bool(1), true);
});

test("a null is never silently a zero", () => {
  assert.equal(price(null), "—");
  assert.equal(pct(null), "—");
  assert.equal(multiple(null), "—");
  assert.equal(score(null), "—");
  assert.equal(shares(null), "—");
  assert.equal(rsi(null), "—");
  assert.equal(crores(null), "—");
  // 0 is a measurement, and it must print.
  assert.equal(price(0), "₹0.00");
  assert.equal(pct(0), "0.00%");
});

test("RSI keeps the decimal that decides a band, and market cap speaks crores", () => {
  assert.equal(rsi(63.25), "63.3");
  assert.equal(rsi(59.96), "60.0");
  assert.equal(crores(1569999), "₹15,69,999 Cr");
  assert.equal(crores(112.4), "₹112 Cr");
});

test("Indian number formatting and units", () => {
  assert.equal(price(1234.5), "₹1,234.50");
  assert.equal(pct(2.71828, 1), "+2.7%");
  assert.equal(pct(-1.23), "-1.23%");
  assert.equal(shares(25_000_000), "2.50 Cr");
  assert.equal(shares(300_000), "3.00 L");
  assert.equal(shares(4_500), "4.5k");
});

test("reason codes stay machine-readable but human-sized", () => {
  assert.equal(humanise("WATCHLIST_ADDED"), "Watchlist added");
  assert.equal(humanise("DATA_FEED_INTERRUPTED"), "Data feed interrupted");
  assert.equal(humanise(""), "—");
  assert.equal(humanise("__"), "__");
});

test("TEXT[] and JSON coercion", () => {
  assert.deepEqual(strings(["A", "B"]), ["A", "B"]);
  assert.deepEqual(strings("{CLOSE_ABOVE_VWAP,EMA9_ABOVE_EMA20}"), ["CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20"]);
  assert.deepEqual(strings(null), []);
  assert.deepEqual(orDash(""), "—");
  assert.deepEqual(orDash(3), "3");
});

test("rowKey is stable per row and cannot collide across prefixes", () => {
  assert.equal(rowKey("signal", 12, 0), "signal-12-0");
  assert.notEqual(rowKey("signal", null, 3), rowKey("candle", null, 3));
});
