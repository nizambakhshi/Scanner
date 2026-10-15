import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import {
  TEST_MESSAGE,
  credentialFingerprint,
  effectiveTelegram,
  mergeTelegramSave,
  sendTelegramMessage,
  telegramCredentialProblem,
  telegramView,
  type TelegramRow,
} from "../lib/telegram";

/**
 * The containment tests for the one credential this app is allowed to hold.
 *
 * `tests/config.test.ts` records that `TELEGRAM_BOT_TOKEN` is read at all; this file
 * is what keeps that reading harmless: the precedence rules, and above all the
 * claim that nothing which leaves this module — a view, a response body, an error
 * sentence — contains the token or the chat id.
 */

const TOKEN = "123456789:AAH_very-secret-token-value";
const CHAT = "-1009876543210";

const row = (overrides: Partial<TelegramRow> = {}): TelegramRow => ({
  enabled: true,
  botToken: TOKEN,
  chatId: CHAT,
  updatedAt: "2026-10-14T21:00:00+05:30",
  ...overrides,
});

test("a saved pair beats the environment, and a missing pair falls back to it", () => {
  const fallback = { token: "999:ENVIRONMENT-TOKEN", chatId: "555555" };
  const saved = effectiveTelegram(row(), fallback);
  assert.equal(saved.source, "saved");
  assert.equal(saved.configured, true);
  assert.equal(saved.token, TOKEN);

  assert.deepEqual(
    { ...effectiveTelegram(null, fallback), token: "", chatId: "" },
    { token: "", chatId: "", source: "env", configured: true, enabled: null },
  );
  assert.equal(effectiveTelegram(null, fallback).token, "999:ENVIRONMENT-TOKEN");

  // Half a row is not a destination: the environment answers instead of a pair
  // that could never deliver.
  const half = effectiveTelegram(row({ chatId: "" }), fallback);
  assert.equal(half.source, "env");
  assert.equal(half.token, "999:ENVIRONMENT-TOKEN");

  const nothing = effectiveTelegram(null, { token: "", chatId: "" });
  assert.equal(nothing.source, "none");
  assert.equal(nothing.configured, false);
});

test("switching Telegram off blanks the pair, so `configured` cannot lie", () => {
  const off = effectiveTelegram(row({ enabled: false }));
  assert.equal(off.token, "");
  assert.equal(off.chatId, "");
  assert.equal(off.configured, false);
  assert.equal(off.source, "saved");
  // The row is still there — the page says so, and the credentials are not lost.
  assert.equal(off.enabled, false);
});

test("no view, response or error string ever carries the token or the chat id", () => {
  const view = telegramView(effectiveTelegram(row(), { token: "", chatId: "" }), row());
  const serialised = JSON.stringify(view);
  assert.equal(serialised.includes(TOKEN), false, "the token must not be serialisable");
  assert.equal(serialised.includes(CHAT), false, "not even the chat id");
  assert.deepEqual(Object.keys(view).sort(), [
    "configured",
    "enabled",
    "fingerprint",
    "hasStoredPair",
    "savedAt",
    "source",
  ]);
  // A disabled row still shows its fingerprint: the credentials exist, pushing does not.
  const disabled = telegramView(effectiveTelegram(row({ enabled: false })), row({ enabled: false }));
  assert.equal(disabled.configured, false);
  assert.equal(disabled.hasStoredPair, true);
  assert.equal(disabled.fingerprint, credentialFingerprint(TOKEN, CHAT));
  assert.equal(telegramView(effectiveTelegram(null), null).fingerprint, null);
});

test("the fingerprint is the worker's construction, so the two can be compared", () => {
  const expected = createHash("sha256").update(`${TOKEN}\n${CHAT}`, "utf8").digest("hex").slice(0, 12);
  assert.equal(credentialFingerprint(TOKEN, CHAT), expected);
  assert.equal(credentialFingerprint(TOKEN, CHAT).length, 12);
  // A different chat id is a different destination, and must look different.
  assert.notEqual(credentialFingerprint(TOKEN, "1234"), expected);
});

test("a blank field keeps what is stored, and clearing is explicit", () => {
  const stored = row();
  const untouched = mergeTelegramSave({ bot_token: "", chat_id: "", enabled: "true" }, stored);
  assert.equal(untouched.botToken, TOKEN);
  assert.equal(untouched.chatId, CHAT);
  assert.equal(untouched.clears, false);

  const replaced = mergeTelegramSave({ bot_token: TOKEN, chat_id: "42" }, stored);
  assert.equal(replaced.chatId, "42");
  assert.equal(replaced.botToken, TOKEN);

  const cleared = mergeTelegramSave({ clear: "true" }, stored);
  assert.equal(cleared.botToken, "");
  assert.equal(cleared.chatId, "");

  // With nothing stored, an empty form is not a save — the route calls that out.
  const fromEmpty = mergeTelegramSave({ bot_token: "", chat_id: "" }, null);
  assert.equal(fromEmpty.botToken, "");
  assert.equal(fromEmpty.enabled, false, "no stored row means the switch stays off, not silently on");

  // The switch arrives as a checkbox: "on"/"true"/"false" all mean what they say.
  assert.equal(mergeTelegramSave({ enabled: "on" }, stored).enabled, true);
  assert.equal(mergeTelegramSave({ enabled: "false" }, stored).enabled, false);
});

test("an impossible credential is caught before it reaches the database", () => {
  assert.equal(telegramCredentialProblem("", ""), null, "nothing being saved is nothing to check");
  assert.match(telegramCredentialProblem(TOKEN, "") ?? "", /both/);
  assert.match(telegramCredentialProblem("not-a-token", CHAT) ?? "", /bot token/);
  assert.match(telegramCredentialProblem(TOKEN, "one-billion") ?? "", /chat id/);
  assert.equal(telegramCredentialProblem(TOKEN, CHAT), null);
  // A group chat id with its leading minus is the common case, not a format error.
  assert.equal(telegramCredentialProblem("123456:AAH_very-secret-token-value", "-1001234567890"), null);
});

/** A `fetch` stand-in: every failure mode returns or throws what the real client does. */
function stubFetch(handler: (init: RequestInit) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>) {
  return (async (input: unknown, init?: RequestInit) => {
    const result = await handler(init ?? {});
    return {
      status: result.status,
      json: result.json,
    } as Response;
  }) as unknown as typeof fetch;
}

test("a successful send reports success and nothing else", async () => {
  const seen: { url?: string; body?: string } = {};
  const result = await sendTelegramMessage(
    TOKEN,
    CHAT,
    TEST_MESSAGE,
    stubFetch((init) => {
      seen.body = String(init.body ?? "");
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    }),
  );
  assert.equal(result.ok, true);
  assert.equal(result.detail.includes(TOKEN), false);
  assert.equal(seen.body?.includes(TOKEN), false, "the token belongs in the URL, never the body");
  assert.equal(seen.body?.includes(CHAT), true, "the destination is in the body, as Telegram requires");
});

test("a rejected token is explained without echoing the URL that carried it", async () => {
  const result = await sendTelegramMessage(
    TOKEN,
    CHAT,
    "x",
    stubFetch(() =>
      Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({ ok: false, description: "Unauthorized" }) }),
    ),
  );
  assert.equal(result.ok, false);
  assert.match(result.detail, /bot token/);
  assert.equal(result.detail.includes(TOKEN), false);
});

test("a chat the bot cannot see is distinguished from a bad token", async () => {
  const result = await sendTelegramMessage(
    TOKEN,
    CHAT,
    "x",
    stubFetch(() =>
      Promise.resolve({
        ok: false,
        status: 400,
        json: () => Promise.resolve({ ok: false, description: "chat not found" }),
      }),
    ),
  );
  assert.equal(result.ok, false);
  assert.match(result.detail, /chat id/);
  assert.match(result.detail, /\/start/);
});

test("a transport failure that echoes the request URL still cannot leak the token", async () => {
  // This is the realistic leak: HTTP clients put the full href — token included —
  // in their error text, and that text is what gets shown to an operator.
  const leaking = (async () => {
    throw new TypeError(`fetch failed: request to https://api.telegram.org/bot${TOKEN}/sendMessage could not be resolved`);
  }) as unknown as typeof fetch;
  const result = await sendTelegramMessage(TOKEN, CHAT, "x", leaking);
  assert.equal(result.ok, false);
  assert.equal(result.detail.includes(TOKEN), false, `leaked in: ${result.detail}`);
  assert.match(result.detail, /Could not reach Telegram/);
});

test("with no credentials there is nothing to send, and no attempt is made", async () => {
  let called = 0;
  const counting = (async () => {
    called += 1;
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
  }) as unknown as typeof fetch;
  assert.equal((await sendTelegramMessage("", CHAT, "x", counting)).ok, false);
  assert.equal((await sendTelegramMessage(TOKEN, "", "x", counting)).ok, false);
  assert.equal(called, 0);
});

test("the test message says it is a test", () => {
  // A test message that looks like an alert would be acted on.
  assert.match(TEST_MESSAGE, /not an alert/);
  assert.equal(TEST_MESSAGE.includes(TOKEN), false);
});
