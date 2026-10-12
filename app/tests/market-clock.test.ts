import { test } from "node:test";
import assert from "node:assert/strict";
import { sessionClock } from "../lib/market-clock";

/**
 * The strip on the Nav is the one element an operator reads before anything they
 * came to read; a wrong phase at 09:14 would say "OPEN" while the scanner is
 * still refusing to judge candles. The clock is pure arithmetic, so the whole
 * schedule is pinned down here.
 *
 * IST is UTC+05:30, and 2026-10-12 is a Monday (2026-10-17 a Saturday).
 */

const at = (iso: string) => new Date(iso);

test("Monday 09:17 IST is the open session", () => {
  const clock = sessionClock(at("2026-10-12T03:47:00Z"));
  assert.equal(clock.phase, "OPEN");
  assert.equal(`${clock.hh}:${clock.mm}:${clock.ss}`, "09:17:00");
  assert.equal(clock.countdown, "closes in 6h 13m");
});

test("the call-auction window is PRE_OPEN, not OPEN", () => {
  assert.equal(sessionClock(at("2026-10-12T03:30:30Z")).phase, "PRE_OPEN"); // 09:00:30 IST
  const before = sessionClock(at("2026-10-12T03:15:00Z")); // 08:45 IST
  assert.equal(before.phase, "CLOSED");
  assert.equal(before.countdown, "opens in 30m");
});

test("09:15:00 sharp flips to OPEN and stops counting to itself", () => {
  const clock = sessionClock(at("2026-10-12T03:45:00Z"));
  assert.equal(clock.phase, "OPEN");
  assert.equal(clock.countdown, "closes in 6h 15m");
});

test("15:30:00 sharp is CLOSED — the last session candle is done", () => {
  assert.equal(sessionClock(at("2026-10-12T09:59:59Z")).phase, "OPEN"); // 15:29:59 IST
  assert.equal(sessionClock(at("2026-10-12T10:00:00Z")).phase, "CLOSED"); // 15:30:00 IST
  assert.equal(sessionClock(at("2026-10-12T09:59:30Z")).countdown, "closes in 1m");
});

test("a weekend counts down to Monday's open", () => {
  const saturday = sessionClock(at("2026-10-17T06:00:00Z")); // Sat 11:30 IST
  assert.equal(saturday.phase, "CLOSED");
  assert.equal(saturday.countdown, "opens Mon 09:15");
});

test("IST midnight rolls the day correctly", () => {
  const clock = sessionClock(at("2026-10-18T18:30:00Z")); // exactly Mon 00:00 IST
  assert.equal(clock.phase, "CLOSED");
  assert.equal(`${clock.hh}:${clock.mm}:${clock.ss}`, "00:00:00");
  assert.equal(clock.countdown, "opens in 9h 15m");
});

test("Friday after the close opens again on Monday, never Saturday", () => {
  const clock = sessionClock(at("2026-10-16T11:00:00Z")); // Fri 16:30 IST
  assert.equal(clock.phase, "CLOSED");
  assert.equal(clock.countdown, "opens Mon 09:15");
});
