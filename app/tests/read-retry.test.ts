import { test } from "node:test";
import assert from "node:assert/strict";
import { isPoolerCapError, isReadTimeout, readRetryDelayMs } from "../lib/db";

/**
 * The production failure this guards: the Supabase session pooler caps clients
 * at 15, and a burst of page loads (several cold serverless instances at once)
 * can trip the cap. The operator then sees "STOCKS DIRECTORY UNAVAILABLE —
 * (EMAXCONNSESSION) max clients reached" on what is otherwise a healthy page.
 * Supavisor decides the refusal before any statement runs, so a read may pause
 * briefly and try once more; writes must never be retried.
 */

const capRefusal = () =>
  new Error("(EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15");

test("a pooler-cap refusal is recognised by its code, not its wording", () => {
  assert.equal(isPoolerCapError(capRefusal()), true);
  assert.equal(isPoolerCapError(new Error("connection is closed")), false);
  // The marker must be inside an Error; a bare string from anywhere else is not a refusal.
  assert.equal(isPoolerCapError("EMAXCONNSESSION"), false);
  assert.equal(isReadTimeout(capRefusal()), false, "a refusal is not a deadline timeout");
});

test("a capped read is retried after a pause, never instantly", () => {
  const delay = readRetryDelayMs(capRefusal(), true);
  assert.equal(typeof delay, "number");
  assert.ok((delay ?? 0) > 0, "a slot needs a moment to free — an instant retry would just be refused again");
});

test("a capped write is never retried", () => {
  assert.equal(readRetryDelayMs(capRefusal(), false), null);
});

test("a deadline read still retries immediately, and its write still never does", () => {
  const timedOut = new Error("database read timed out after 3500ms");
  assert.equal(readRetryDelayMs(timedOut, true), 0);
  assert.equal(readRetryDelayMs(timedOut, false), null);
});

test("ordinary driver errors are handed straight to the caller", () => {
  assert.equal(readRetryDelayMs(new Error('relation "candles_1d" does not exist'), true), null);
  assert.equal(readRetryDelayMs(undefined, true), null);
});
