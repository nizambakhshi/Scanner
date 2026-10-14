/**
 * Telegram credentials and the one message this app is allowed to send.
 *
 * The operator's decision (2026-10-14): the bot token and chat id live in
 * `ops_settings['telegram']`, are edited on /alerts, and are never shown back.
 * The environment stays as a fallback, which is what the worker also does, so
 * the two processes can agree on one destination without anyone having to
 * redeploy.
 *
 * The rules this file exists to keep:
 *
 * * **The token leaves this module only as an outbound HTTPS call.** No view
 *   function returns it, and nothing about it is derived from it except a
 *   twelve-hex SHA-256 fingerprint of the `token\nchat_id` pair — the same
 *   construction the worker's `telegram_credentials_applied` log line prints, so
 *   an operator can prove the page and the running worker hold the same pair.
 * * **Blank means "keep what is stored"**, because a form that has to be
 *   re-typed every save is a form that quietly wipes the destination.
 * * **The dashboard sends exactly one kind of message**: the test the operator
 *   asks for. Real alerts are raised, deduped and delivered by the worker — a
 *   second sender would be a second cooldown table, and the two would disagree.
 * * **Errors are scrubbed before they are shown.** An HTTP failure can carry the
 *   request URL, and this URL contains the token.
 */

import { createHash } from "node:crypto";
import { env } from "./env";
import { redact } from "./redact";

export const TELEGRAM_SETTINGS_KEY = "telegram";

/** The stored document. `enabled` is a real boolean: the worker gates on it. */
export type TelegramRow = {
  enabled: boolean;
  botToken: string;
  chatId: string;
  updatedAt: string | null;
};

/** Where the effective pair came from, and whether it is usable. */
export type EffectiveTelegram = {
  token: string;
  chatId: string;
  /** `saved` — the database row, `env` — the process environment, `none` — neither. */
  source: "saved" | "env" | "none";
  configured: boolean;
  /** The row's own switch, when the row exists. Null when nothing is stored. */
  enabled: boolean | null;
};

/**
 * What a screen or an API response may carry about the credentials. There is
 * deliberately no token field and no chat id field: `fingerprint` is a twelve-hex
 * SHA-256 prefix, which lets an operator match the page against the worker's
 * `telegram_credentials_applied` log line without either one revealing a secret.
 * A view type that carried the token would eventually be spread into a response
 * by someone who meant well.
 */
export type TelegramView = {
  /** `saved` — the database row, `env` — the process environment, `none` — neither. */
  source: EffectiveTelegram["source"];
  /** True when the worker would be able to push right now. */
  configured: boolean;
  /** The row's own switch; null when no row exists. */
  enabled: boolean | null;
  /** True when a *complete* pair is stored in the database, switched off or not. */
  hasStoredPair: boolean;
  fingerprint: string | null;
  savedAt: string | null;
};

/** `hashlib.sha256(f"{token}\n{chat_id}")[:12]` — identical to the worker. */
export function credentialFingerprint(token: string, chatId: string): string {
  return createHash("sha256").update(`${token}\n${chatId}`, "utf8").digest("hex").slice(0, 12);
}

/**
 * Resolve the stored row against the environment, exactly the way
 * `WorkerRuntime._adopt_alert_settings` does.
 *
 * A row with `enabled: false` *blanks* the pair rather than leaving it
 * configured: `configured` is what the alert engine gates on, so a switch that
 * says off must mean off. A row with only half a pair is ignored, because a
 * token with no chat id cannot deliver anything and would look configured on the
 * page while the worker stayed silent.
 *
 * `fallback` is the environment pair, injectable so a test can prove the
 * precedence without editing this process's variables.
 */
export function effectiveTelegram(
  row: TelegramRow | null,
  fallback: { token: string; chatId: string } = { token: env.telegramBotToken, chatId: env.telegramChatId },
): EffectiveTelegram {
  const envPair = { token: fallback.token.trim(), chatId: fallback.chatId.trim() };
  if (row && row.botToken !== "" && row.chatId !== "") {
    const off = row.enabled === false;
    return {
      token: off ? "" : row.botToken,
      chatId: off ? "" : row.chatId,
      source: "saved",
      configured: !off,
      enabled: row.enabled,
    };
  }
  const configured = envPair.token !== "" && envPair.chatId !== "";
  return {
    token: configured ? envPair.token : "",
    chatId: configured ? envPair.chatId : "",
    source: configured ? "env" : "none",
    configured,
    enabled: row ? row.enabled : null,
  };
}

/**
 * The only Telegram shape that may cross a render boundary or an API response:
 * a few flags, a timestamp, and a fingerprint. No token, no chat id.
 */
export function telegramView(effective: EffectiveTelegram, row: TelegramRow | null = null): TelegramView {
  // The fingerprint is of the *stored* pair when a complete one exists, switched
  // off or not: an operator compares it against the worker's log line to prove the
  // two hold the same credentials, and a row that is merely disabled must not
  // render as though nothing was saved.
  const hasStoredPair = Boolean(row && row.botToken !== "" && row.chatId !== "");
  const pair = hasStoredPair
    ? { token: row?.botToken ?? "", chatId: row?.chatId ?? "" }
    : effective.configured && effective.token !== "" && effective.chatId !== ""
      ? { token: effective.token, chatId: effective.chatId }
      : null;
  return {
    source: effective.source,
    configured: effective.configured,
    enabled: effective.enabled,
    hasStoredPair,
    fingerprint: pair ? credentialFingerprint(pair.token, pair.chatId) : null,
    savedAt: row?.updatedAt ?? null,
  };
}

/**
 * Merge an editor submission into what is already stored.
 *
 * Empty token or chat id keeps the stored value. `clear` is the explicit escape
 * hatch: the operator has to ask for the credentials to be removed, which is the
 * difference between a form mistake and a deliberate step back to the environment
 * fallback.
 */
export function mergeTelegramSave(
  form: Record<string, string>,
  current: TelegramRow | null,
): { botToken: string; chatId: string; enabled: boolean; clears: boolean } {
  const clears = /^(1|true|yes|on)$/i.test((form.clear ?? "").trim());
  const token = (form.bot_token ?? "").trim();
  const chat = (form.chat_id ?? "").trim();
  const enabledRaw = form.enabled;
  const enabled =
    enabledRaw === undefined ? (current?.enabled ?? false) : /^(1|true|yes|on)$/i.test(String(enabledRaw).trim());
  if (clears) return { botToken: "", chatId: "", enabled, clears: true };
  return {
    botToken: token === "" ? (current?.botToken ?? "") : token,
    chatId: chat === "" ? (current?.chatId ?? "") : chat,
    enabled,
    clears: false,
  };
}

/** A bot token is `digits:40-ish-url-safe`; a chat id is a number, often negative. */
export function telegramCredentialProblem(token: string, chatId: string): string | null {
  if (token === "" && chatId === "") return null;
  if (token === "" || chatId === "") {
    return "A bot token and a chat id are both needed — one without the other cannot deliver.";
  }
  if (!/^\d{6,10}:[A-Za-z0-9_-]{20,}$/.test(token)) {
    return "That does not look like a bot token. Telegram's format is digits, a colon, then the secret.";
  }
  if (!/^-?\d{4,20}$/.test(chatId)) {
    return "That does not look like a chat id. It is a number — a group id starts with a minus sign.";
  }
  return null;
}

export type SendResult = { ok: boolean; detail: string };

const SEND_TIMEOUT_MS = 8000;

/**
 * The test message. `POST https://api.telegram.org/bot<token>/sendMessage`.
 *
 * Never throws: whatever happens, the caller gets a short, token-free sentence,
 * because that sentence is what the operator reads on the page and what an audit
 * row could carry. `scrub` removes the token from the string *before* the generic
 * redaction runs — a library that echoes the request URL back in its error would
 * otherwise hand a credential to a browser.
 */
export async function sendTelegramMessage(
  token: string,
  chatId: string,
  text: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SendResult> {
  if (token === "" || chatId === "") return { ok: false, detail: "No bot token and chat id to send with." };
  const scrub = (value: string) => redact(value.split(token).join("••••"));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const response = await fetchImpl(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
      signal: controller.signal,
    });
    let payload: { ok?: boolean; description?: string } | null = null;
    try {
      payload = (await response.json()) as { ok?: boolean; description?: string } | null;
    } catch {
      payload = null;
    }
    const description = payload?.description ? scrub(String(payload.description)).slice(0, 120) : "";
    if (payload?.ok === true) {
      return { ok: true, detail: "Telegram accepted the message. Check the chat it was sent to." };
    }
    if (response.status === 401 || response.status === 403) {
      return { ok: false, detail: `Telegram rejected the bot token (${description || response.status}).` };
    }
    if (response.status === 400 && /chat/i.test(description)) {
      return {
        ok: false,
        detail: `The token works but this chat id does not (${description}). Send /start to the bot first, then check the id.`,
      };
    }
    return {
      ok: false,
      detail: `Telegram replied HTTP ${response.status}${description ? `: ${description}` : "."}`,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (controller.signal.aborted) return { ok: false, detail: `Telegram did not answer within ${SEND_TIMEOUT_MS / 1000}s.` };
    return { ok: false, detail: `Could not reach Telegram: ${scrub(message).slice(0, 120)}` };
  } finally {
    clearTimeout(timer);
  }
}

/** The message the test button sends. Honest about being a test — a fake signal
 *  would be acted on. */
export const TEST_MESSAGE = [
  "Bullish Scanner: this is a test message, not an alert.",
  "If you can read this, the bot token and chat id in the dashboard are correct.",
  "Real alerts come from the worker: session high/low, opening range and VWAP breaks.",
].join("\n");
