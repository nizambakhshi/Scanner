import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import {
  TEST_MESSAGE,
  chatsFingerprint,
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
 * sentence — contains the bot token.
 *
 * The channel list is a deliberate exception, since 2026-10-16: the /alerts page
 * shows the chat ids so the operator can delete a channel they can see. The
 * *token* has no such need and never crosses a boundary.
 */

const TOKEN = "123456789:AAH_very-secret-token-value";
const CHAT = "-1009876543210";
const CHAT2 = "5551234";

const row = (overrides: Partial<TelegramRow> = {}): TelegramRow => ({
  enabled: true,
  botToken: TOKEN,
  chats: [{ chatId: CHAT, label: null }],
  updatedAt: "2026-10-14T21:00:00+05:30",
  ...overrides,
});

test("a saved set beats the environment, and a missing set falls back to it", () => {
  const fallback = { token: "999:ENVIRONMENT-TOKEN", chatId: "55555" };
  const saved = effectiveTelegram(row(), fallback);
  assert.equal(saved.source, "saved");
  assert.equal(saved.configured, true);
  assert.equal(saved.token, TOKEN);
  assert.deepEqual(saved.chats, [{ chatId: CHAT, label: null }]);

  assert.deepEqual(
    { ...effectiveTelegram(null, fallback), token: "", chats: [] },
    { token: "", chats: [], source: "env", configured: true, enabled: null },
  );
  assert.equal(effectiveTelegram(null, fallback).chats[0]?.chatId, "55555");

  // A token with no destination is not a set: the environment answers instead of
  // a pair that could never deliver.
  const noChats = effectiveTelegram(row({ chats: [] }), fallback);
  assert.equal(noChats.source, "env");
  assert.equal(noChats.token, "999:ENVIRONMENT-TOKEN");

  const nothing = effectiveTelegram(null, { token: "", chatId: "" });
  assert.equal(nothing.source, "none");
  assert.equal(nothing.configured, false);
  assert.deepEqual(nothing.chats, []);
});

test("switching Telegram off blanks the set, so `configured` cannot lie", () => {
  const off = effectiveTelegram(row({ enabled: false }));
  assert.equal(off.token, "");
  assert.deepEqual(off.chats, []);
  assert.equal(off.configured, false);
  assert.equal(off.source, "saved");
  // The row is still there — the page says so, and the credentials are not lost.
  assert.equal(off.enabled, false);
});

test("no view, response or error string ever carries the token", () => {
  const view = telegramView(effectiveTelegram(row(), { token: "", chatId: "" }), row());
  const serialised = JSON.stringify(view);
  assert.equal(serialised.includes(TOKEN), false, "the token must not be serialisable");
  assert.deepEqual(Object.keys(view).sort(), [
    "chats",
    "configured",
    "enabled",
    "fingerprint",
    "hasStoredPair",
    "savedAt",
    "source",
  ]);
  // The channels are visible — that is the point of the panel's Delete buttons.
  assert.deepEqual(view.chats, [{ chatId: CHAT, label: null }]);
  // A disabled row still shows its fingerprint: the credentials exist, pushing does not.
  const disabled = telegramView(effectiveTelegram(row({ enabled: false })), row({ enabled: false }));
  assert.equal(disabled.configured, false);
  assert.equal(disabled.hasStoredPair, true);
  assert.equal(disabled.fingerprint, chatsFingerprint(TOKEN, [{ chatId: CHAT, label: null }]));
  assert.equal(telegramView(effectiveTelegram(null), null).fingerprint, null);
});

test("the fingerprint is the worker's construction, so the two can be compared", () => {
  const expected = createHash("sha256").update(`${TOKEN}\n${CHAT}`, "utf8").digest("hex").slice(0, 12);
  assert.equal(credentialFingerprint(TOKEN, CHAT), expected);
  assert.equal(credentialFingerprint(TOKEN, CHAT).length, 12);
  // One channel fingerprints identically to the old single-pair document, so a
  // migrated row still matches the worker's log line.
  assert.equal(chatsFingerprint(TOKEN, [{ chatId: CHAT, label: null }]), expected);
  // Several channels join with commas; a different set is a different destination.
  assert.equal(
    chatsFingerprint(TOKEN, [{ chatId: CHAT, label: null }, { chatId: CHAT2, label: "team" }]),
    createHash("sha256").update(`${TOKEN}\n${CHAT},${CHAT2}`, "utf8").digest("hex").slice(0, 12),
  );
  assert.notEqual(chatsFingerprint(TOKEN, [{ chatId: CHAT2, label: null }]), expected);
  assert.equal(chatsFingerprint("", [{ chatId: CHAT, label: null }]), null);
  assert.equal(chatsFingerprint(TOKEN, []), null);
});

test("a blank token keeps what is stored, the channel list replaces, and clearing is explicit", () => {
  const stored = row({ chats: [{ chatId: CHAT, label: "Alerts" }] });
  const untouched = mergeTelegramSave({ botToken: "", chats: null, enabled: true, clears: false }, stored);
  assert.equal(untouched.botToken, TOKEN);
  assert.deepEqual(untouched.chats, [{ chatId: CHAT, label: "Alerts" }]);
  assert.equal(untouched.clears, false);

  // The panel posts the list it shows: an added row appears, a deleted row goes.
  const replaced = mergeTelegramSave(
    { botToken: "", chats: [{ chatId: CHAT, label: null }, { chatId: CHAT2, label: "Team" }], enabled: null, clears: false },
    stored,
  );
  assert.deepEqual(replaced.chats, [{ chatId: CHAT, label: null }, { chatId: CHAT2, label: "Team" }]);
  assert.equal(replaced.botToken, TOKEN);

  const cleared = mergeTelegramSave({ botToken: "", chats: null, enabled: null, clears: true }, stored);
  assert.equal(cleared.botToken, "");
  assert.deepEqual(cleared.chats, []);

  // With nothing stored, an empty form is not a save — the route calls that out.
  const fromEmpty = mergeTelegramSave({ botToken: "", chats: null, enabled: null, clears: false }, null);
  assert.equal(fromEmpty.botToken, "");
  assert.deepEqual(fromEmpty.chats, []);
  assert.equal(fromEmpty.enabled, false, "no stored row means the switch stays off, not silently on");

  // The switch travels as a real boolean or an explicit null ("keep stored").
  assert.equal(mergeTelegramSave({ botToken: "", chats: null, enabled: false, clears: false }, stored).enabled, false);
});

test("an impossible credential is caught before it reaches the database", () => {
  const one = [{ chatId: CHAT, label: null }];
  assert.equal(telegramCredentialProblem("", []), null, "nothing being saved is nothing to check");
  assert.match(telegramCredentialProblem(TOKEN, []) ?? "", /both/);
  assert.match(telegramCredentialProblem("", one) ?? "", /both/);
  assert.match(telegramCredentialProblem("not-a-token", one) ?? "", /bot token/);
  assert.match(telegramCredentialProblem(TOKEN, [{ chatId: "one-billion", label: null }]) ?? "", /chat id/);
  assert.equal(telegramCredentialProblem(TOKEN, one), null);
  // A group chat id with its leading minus is the common case, not a format error.
  assert.equal(telegramCredentialProblem("123456:AAH_very-secret-token-value", [{ chatId: "-1001234567890", label: null }]), null);
  // The same chat twice would page the operator twice.
  assert.match(
    telegramCredentialProblem(TOKEN, [{ chatId: CHAT, label: null }, { chatId: CHAT, label: "copy" }]) ?? "",
    /twice/,
  );
  // A valid multi-channel set passes.
  assert.equal(telegramCredentialProblem(TOKEN, [{ chatId: CHAT, label: null }, { chatId: CHAT2, label: "Team" }]), null);
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
  const seen: { body?: string } = {};
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
