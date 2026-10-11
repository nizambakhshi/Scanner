import { test } from "node:test";
import assert from "node:assert/strict";
import { isReadTimeout, withReadDeadline } from "../lib/db";

/**
 * The serverless failure this guards: a Vercel function instance thaws with a
 * TCP socket the pooler has already dropped, writes the next query into it, and
 * waits for a reply that never arrives. `/health` hung past 150s exactly this
 * way while every individual query stayed at ~25ms. The deadline is the fix,
 * so it needs its own tests rather than only page coverage.
 */

const tick = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test("a read that never resolves rejects at the deadline", async () => {
  const started = Date.now();
  await assert.rejects(
    withReadDeadline(new Promise<string>(() => {}), 25),
    (error: unknown) => isReadTimeout(error),
  );
  assert.ok(Date.now() - started < 1000, "the rejection must arrive at the deadline, not later");
});

test("a healthy read passes through untouched", async () => {
  assert.equal(await withReadDeadline(Promise.resolve("rows"), 500), "rows");
});

test("a driver error that arrives first is the rejection the caller sees", async () => {
  await assert.rejects(withReadDeadline(Promise.reject(new Error("connection is closed")), 500), /connection is closed/);
  assert.equal(isReadTimeout(new Error("connection is closed")), false);
});

test("a rejection arriving after the deadline was lost cannot crash the process", async () => {
  let abandon!: (error: Error) => void;
  const pending = new Promise<never>((_, reject) => {
    abandon = reject;
  });
  await assert.rejects(withReadDeadline(pending, 10), (error: unknown) => isReadTimeout(error));
  abandon(new Error("too late for the race, must stay handled"));
  await tick(40); // an unhandled rejection here would fail the test run itself
});
