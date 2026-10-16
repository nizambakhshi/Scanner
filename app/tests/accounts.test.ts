import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BASIC_WATCHLIST_LIMIT,
  capForPlan,
  createAccountProblem,
  emailProblem,
  isPlan,
  normalisePlan,
  subscribeProblem,
} from "../lib/accounts";

/**
 * The paywall's rules, tested as pure functions: this file is the whole reason
 * a BASIC account and an UNLIMITED account can never disagree about what three
 * rows means — the page and the tests call the same `capForPlan`.
 */

test("plans are spelled one way, whatever the form sends", () => {
  assert.equal(isPlan("BASIC"), true);
  assert.equal(isPlan("unlimited"), false, "the stored vocabulary is upper-case; isPlan is the strict check");
  assert.equal(isPlan("PRO"), false);
  assert.equal(normalisePlan(" unlimited "), "UNLIMITED");
  assert.equal(normalisePlan("basic"), "BASIC");
  assert.equal(normalisePlan(undefined), "BASIC", "no answer is the conservative plan");
});

test("an email is checked the way the login form checks it", () => {
  assert.equal(emailProblem("trader@example.com"), null);
  assert.equal(emailProblem("  trader@example.com  "), null, "surrounding space is not the user's problem");
  assert.match(emailProblem("") ?? "", /Enter/);
  assert.match(emailProblem("trader@") ?? "", /does not look/);
  assert.match(emailProblem("a b@example.com") ?? "", /does not look/);
});

test("an admin may create any role; the public form could never reach this", () => {
  assert.equal(createAccountProblem({ email: "a@b.co", role: "USER", plan: "BASIC" }), null);
  assert.equal(createAccountProblem({ email: "a@b.co", role: "SUPER_ADMIN", plan: "UNLIMITED" }), null);
  assert.match(createAccountProblem({ email: "a@b.co", role: "OWNER", plan: "BASIC" }) ?? "", /role/);
  assert.match(createAccountProblem({ email: "a@b.co", role: "USER", plan: "FREE" }) ?? "", /plan/);
  assert.match(createAccountProblem({ email: "nope", role: "USER", plan: "BASIC" }) ?? "", /email/);
});

test("a public request asks for a plan and keeps the note short", () => {
  assert.equal(subscribeProblem({ email: "a@b.co", plan: "UNLIMITED", note: "" }), null);
  assert.match(subscribeProblem({ email: "a@b.co", plan: "ADMIN", note: "" }) ?? "", /plan/);
  assert.match(subscribeProblem({ email: "a@b.co", plan: "BASIC", note: "x".repeat(501) }) ?? "", /500/);
});

test("BASIC sees the top three, everyone else sees the list", () => {
  const rows = [1, 2, 3, 4, 5, 6];
  assert.deepEqual(capForPlan(rows, "BASIC"), [1, 2, 3]);
  assert.deepEqual(capForPlan(rows, "UNLIMITED"), rows);
  assert.deepEqual(capForPlan(rows, "UNKNOWN"), rows, "a database without the plan column behaves as UNLIMITED");
  assert.equal(BASIC_WATCHLIST_LIMIT, 3, "the spec's number, in one place");
  assert.deepEqual(capForPlan([1, 2], "BASIC"), [1, 2], "a short list passes through untouched");
  assert.equal(capForPlan([], "BASIC").length, 0);
});
