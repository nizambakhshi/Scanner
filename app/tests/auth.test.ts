import "./env-setup";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { hashPassword, passwordProblem, verifyPassword } from "../lib/auth/password";
import { issueToken, readToken, SESSION_COOKIE, type SessionPayload } from "../lib/auth/session";

test("the cookie is named for this app and is not a guessable default", () => {
  assert.equal(SESSION_COOKIE, "scanner_session");
});

test("a hashed password verifies, and a different one does not", async () => {
  const stored = await hashPassword("correct horse battery staple");
  assert.match(stored, /^scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}$/);
  assert.equal(await verifyPassword("correct horse battery staple", stored), true);
  assert.equal(await verifyPassword("Correct horse battery staple", stored), false);
});

test("two hashes of the same password differ, because the salt is random", async () => {
  const a = await hashPassword("repeatable-not-repeated");
  const b = await hashPassword("repeatable-not-repeated");
  assert.notEqual(a, b);
  assert.equal(await verifyPassword("repeatable-not-repeated", b), true);
});

test("a malformed stored hash is rejected rather than treated as empty", async () => {
  assert.equal(await verifyPassword("anything", ""), false);
  assert.equal(await verifyPassword("anything", null), false);
  assert.equal(await verifyPassword("anything", "bcrypt$deadbeef"), false);
  assert.equal(await verifyPassword("anything", "scrypt$onlyonepart"), false);
});

test("the password rules reject the obvious ones", () => {
  assert.equal(passwordProblem("short"), "Use at least 10 characters.");
  assert.equal(passwordProblem("aaaaaaaaaaaa"), "That looks too repetitive to be a password.");
  assert.equal(passwordProblem("password12345"), "That is a commonly used password.");
  assert.equal(passwordProblem("a".repeat(201)), "That password is too long to be stored safely.");
  assert.equal(passwordProblem("T3-rail-station-ledger"), null);
});

test("a session token round-trips its payload exactly", () => {
  const payload: SessionPayload = {
    sub: "1",
    email: "operator@example.com",
    role: "SUPER_ADMIN",
    exp: Math.floor(Date.now() / 1000) + 3600,
    jti: "11111111-2222-3333-4444-555555555555",
  };
  assert.deepEqual(readToken(issueToken(payload)), payload);
});

test("editing the payload without the secret invalidates the token", () => {
  const token = issueToken({
    sub: "1",
    email: "operator@example.com",
    role: "SUPER_ADMIN",
    exp: Math.floor(Date.now() / 1000) + 3600,
    jti: "aaaa-bbbb",
  });
  const forged = Buffer.from(
    JSON.stringify({ sub: "1", email: "attacker@example.com", role: "SUPER_ADMIN", exp: Math.floor(Date.now() / 1000) + 9999, jti: "aaaa-bbbb" }),
    "utf8",
  ).toString("base64url");
  const signature = token.split(".")[1];
  assert.equal(readToken(`${forged}.${signature}`), null);
  assert.equal(readToken(`${token.slice(0, -2)}aa`), null);
  assert.equal(readToken("nonsense"), null);
  assert.equal(readToken(undefined), null);
});

test("an expired token is refused even with a valid signature", () => {
  const token = issueToken({
    sub: "1",
    email: "operator@example.com",
    role: "SUPER_ADMIN",
    exp: Math.floor(Date.now() / 1000) - 1,
    jti: "aaaa-bbbb",
  });
  assert.equal(readToken(token), null);
});

test("a correctly signed token with an unusable payload is still refused", () => {
  const secret = Buffer.from("0123456789abcdef0123456789abcdef", "utf8");
  // No `sub`, no `jti`: nothing a database session row could be matched to.
  const data = Buffer.from(JSON.stringify({ email: "operator@example.com", exp: Math.floor(Date.now() / 1000) + 60 }), "utf8").toString("base64url");
  const signature = createHmac("sha256", secret).update(data).digest("base64url");
  assert.equal(readToken(`${data}.${signature}`), null);

  const shortSecret = Buffer.from("too-short-to-be-a-secret", "utf8");
  const weak = createHmac("sha256", shortSecret).update(data).digest("base64url");
  assert.equal(readToken(`${data}.${weak}`), null);
});
