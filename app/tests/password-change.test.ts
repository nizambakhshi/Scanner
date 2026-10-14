import "./env-setup";
import assert from "node:assert/strict";
import { test } from "node:test";

import { changePasswordProblem, hashPassword, passwordProblem, verifyPassword } from "../lib/auth/password";

/**
 * The rules that decide whether a password *change* is allowed, before any database
 * is touched. The interesting cases are the ones where a change would be a no-op with
 * a success message — those are exactly what an operator would otherwise learn about
 * only when the old password stopped working somewhere else.
 */

test("a change needs both fields, and says which one is missing", () => {
  assert.equal(changePasswordProblem("", "T3-rail-station-ledger"), "Enter your current password.");
  assert.equal(changePasswordProblem("T3-rail-station-ledger", ""), "Enter a new password.");
  assert.equal(changePasswordProblem("", ""), "Enter your current password.");
});

test("changing a password to itself is refused, not reported as success", () => {
  const same = "T3-rail-station-ledger";
  assert.equal(changePasswordProblem(same, same), "That is the same as your current password.");
});

test("the sameness check sees through the normalisation the hash applies", () => {
  // `normalise()` NFKCs and strips a trailing space before deriving the key, so a
  // trailing space is the *same* password, not a new one. Without this the form would
  // claim a change that hashing had already undone.
  assert.equal(changePasswordProblem("ledger-key", "ledger-key "), "That is the same as your current password.");
  assert.equal(changePasswordProblem("ledger-key ", "ledger-key"), "That is the same as your current password.");
});

test("the new password has to pass the strength rules, and the current one does not", () => {
  // A long-standing weak password must still be changeable; only the replacement is
  // judged. Rejecting `currentPassword` here would trap someone behind the very
  // password this form exists to replace.
  assert.equal(changePasswordProblem("short", "T3-rail-station-ledger"), null);
  assert.equal(changePasswordProblem("T3-rail-station-ledger", "short"), "Use at least 10 characters.");
  assert.equal(changePasswordProblem("T3-rail-station-ledger", "password12345"), "That is a commonly used password.");
  assert.equal(changePasswordProblem("T3-rail-station-ledger", "a".repeat(201)), "That password is too long to be stored safely.");
});

test("a good change returns null, and the accepted form is hashable as-is", async () => {
  const current = "old-turnstile-77";
  const next = "T3-rail-station-ledger";
  assert.equal(changePasswordProblem(current, next), null);
  const stored = await hashPassword(next);
  assert.equal(await verifyPassword(next, stored), true);
  // The old password stops being a way in, which is the whole point.
  assert.equal(await verifyPassword(current, stored), false);
});

test("change rules and sign-in rules are the same rules for the new password", () => {
  for (const candidate of ["short", "aaaaaaaaaaaa", "password12345", "q".repeat(201), "T3-rail-station-ledger"]) {
    assert.equal(changePasswordProblem("anything-current", candidate), passwordProblem(candidate));
  }
});
