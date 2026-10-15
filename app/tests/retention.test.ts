import { test } from "node:test";
import assert from "node:assert/strict";
import { purgeWindow, savedWindow, windowForDisplay } from "../lib/ops";

/**
 * The ageing window has three jobs, and they must never bleed into each other:
 * the read path must show what was saved, the save path must be able to store
 * "keep forever" (0), and the delete path must never execute it. The failure
 * these pin is silent and destructive: `Number(0)` clamped up to 1 turns the
 * Mini's "accumulate history" setting into "delete everything older than a
 * day" on the next sweep — candles the worker then has to re-fetch.
 */

test("a saved 0 survives the read path as keep-forever", () => {
  assert.equal(windowForDisplay(0), 0);
  assert.equal(windowForDisplay(-3), 0);
  assert.equal(windowForDisplay(7), 7);
  assert.equal(windowForDisplay(45), 30); // the read path caps, it does not invent
});

test("an unreadable stored window is the default, not 'forever'", () => {
  // `Number(null)` and `Number("")` both coerce to 0. Reading either as
  // keep-forever would show an off switch the worker — which falls back to
  // RETENTION_DAYS for exactly these values — is not honouring.
  assert.equal(windowForDisplay(null), null);
  assert.equal(windowForDisplay(undefined), null);
  assert.equal(windowForDisplay(""), null);
  assert.equal(windowForDisplay("junk"), null);
  assert.equal(windowForDisplay("4"), 4); // a legacy string write still reads
});

test("a purge refuses 0 instead of clamping it up to a one-day delete", () => {
  assert.equal(purgeWindow(3), 3);
  assert.throws(() => purgeWindow(0), /days must be/);
  assert.throws(() => purgeWindow(-1), /days must be/);
  assert.throws(() => purgeWindow(31), /days must be/);
  assert.throws(() => purgeWindow(Number.NaN), /days must be/);
});

test("a window save accepts 0 and 1..30, and nothing else", () => {
  assert.equal(savedWindow(0), 0);
  assert.equal(savedWindow(1), 1);
  assert.equal(savedWindow(30), 30);
  assert.throws(() => savedWindow(-1), /days must be/);
  assert.throws(() => savedWindow(31), /days must be/);
  assert.throws(() => savedWindow(Number.NaN), /days must be/);
});
