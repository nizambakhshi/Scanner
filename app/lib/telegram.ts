/**
 * Telegram credentials and the one message this app is allowed to send.
 *
 * The operator's decision (2026-10-14, channels added 2026-10-16): the bot token
 * and the channel list live in `ops_settings['telegram']`, are edited on /alerts,
 * and the token is never shown back. The environment stays as a fallback, which
 * is what the worker also does, so the two processes can agree on one destination
 * without anyone having to redeploy.
 *
 * The rules this file exists to keep:
 *
 * * **The token leaves this module only as an outbound HTTPS call.** No view
 *   function returns it, and nothing about it is derived from it except a
 *   twelve-hex SHA-256 fingerprint of the `token\n<chat ids joined by commas>`
 *   pair — for exactly one channel this is byte-identical to the old pair
 *   fingerprint, so a migrated row still matches the worker's
 *   `telegram_credentials_applied` log line.
 * * **Chat ids are visible on the admin page, the token never is.** A channel
 *   list you cannot read is a list you cannot delete from; the token has no
 *   such need. Audit rows carry a count and the fingerprint, never the ids.
 * * **Blank token means "keep what is stored"**; the channel list arrives
 *   **complete** on every save (Add and Delete buttons edit it in place), because
 *   a partial list could never be merged sensibly.
 * * **The stored document mirrors the first channel into `chat_id`.** The live
 *   worker reads that field; a chats-only document would look half-saved to it
 *   and fall back to the environment — a silent destination change.
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

/** One push destination. The label is the operator's memo ("Alerts", "Team"). */
export type TelegramChat = { chatId: string; label: string | null };

/** The stored document. `enabled` is a real boolean: the worker gates on it. */
export type TelegramRow = {
  enabled: boolean;
  botToken: string;
  /** All push destinations. Never empty when `botToken` is set and the row is usable. */
  chats: TelegramChat[];
  updatedAt: string | null;
};

/** Where the effective credentials came from, and whether they are usable. */
export type EffectiveTelegram = {
  token: string;
  chats: TelegramChat[];
  /** `saved` — the database row, `env` — the process environment, `none` — neither. */
  source: "saved" | "env" | "none";
  configured: boolean;
  /** The row's own switch, when the row exists. Null when nothing is stored. */
  enabled: boolean | null;
};

/**
 * What a screen or an API response may carry about the credentials: flags, a
 * timestamp, the channel list, and a fingerprint. No token, ever.
 */
export type TelegramView = {
  /** `saved` — the database row, `env` — the process environment, `none` — neither. */
  source: EffectiveTelegram["source"];
  /** True when the worker would be able to push right now. */
  configured: boolean;
  /** The row's own switch; null when no row exists. */
  enabled: boolean | null;
  /** True when a *complete* token + channel pair is stored, switched off or not. */
  hasStoredPair: boolean;
  /** The effective destinations — visible so the page can offer Delete per row. */
  chats: TelegramChat[];
  fingerprint: string | null;
  savedAt: string | null;
};

/** `hashlib.sha256(f"{token}\n{chat_ids}")[:12]` — identical to the worker. */
export function credentialFingerprint(token: string, chatId: string): string {
  return createHash("sha256").update(`${token}\n${chatId}`, "utf8").digest("hex").slice(0, 12);
}

/**
 * Fingerprint of a whole channel set: the ids joined by commas, then the same
 * hash as the single-chat pair. One channel therefore produces the identical
 * twelve hexes the old documents and the worker's log line produce.
 */
export function chatsFingerprint(token: string, chats: TelegramChat[]): string | null {
  if (token === "" || chats.length === 0) return null;
  return credentialFingerprint(token, chats.map((chat) => chat.chatId).join(","));
}

/**
 * Resolve the stored row against the environment, exactly the way
 * `WorkerRuntime._adopt_alert_settings` does.
 *
 * A row with `enabled: false` *blanks* the pair rather than leaving it
 * configured: `configured` is what the alert engine gates on, so a switch that
 * says off must mean off. A row with a token and no channels is ignored, because
 * a token with no destination cannot deliver anything and would look configured
 * on the page while the worker stayed silent.
 *
 * `fallback` is the environment pair, injectable so a test can prove the
 * precedence without editing this process's variables.
 */
export function effectiveTelegram(
  row: TelegramRow | null,
  fallback: { token: string; chatId: string } = { token: env.telegramBotToken, chatId: env.telegramChatId },
): EffectiveTelegram {
  const envPair = { token: fallback.token.trim(), chatId: fallback.chatId.trim() };
  if (row && row.botToken !== "" && row.chats.length > 0) {
    const off = row.enabled === false;
    return {
      token: off ? "" : row.botToken,
      chats: off ? [] : row.chats,
      source: "saved",
      configured: !off,
      enabled: row.enabled,
    };
  }
  const configured = envPair.token !== "" && envPair.chatId !== "";
  return {
    token: configured ? envPair.token : "",
    chats: configured ? [{ chatId: envPair.chatId, label: "environment" }] : [],
    source: configured ? "env" : "none",
    configured,
    enabled: row ? row.enabled : null,
  };
}

/**
 * The only Telegram shape that may cross a render boundary or an API response:
 * a few flags, a timestamp, the channel list, and a fingerprint. No token.
 */
export function telegramView(effective: EffectiveTelegram, row: TelegramRow | null = null): TelegramView {
  // The fingerprint is of the *stored* set when a complete one exists, switched
  // off or not: an operator compares it against the worker's log line to prove the
  // two hold the same credentials, and a row that is merely disabled must not
  // render as though nothing was saved.
  const hasStoredPair = Boolean(row && row.botToken !== "" && row.chats.length > 0);
  const pair = hasStoredPair
    ? { token: row?.botToken ?? "", chats: row?.chats ?? [] }
    : effective.configured && effective.token !== "" && effective.chats.length > 0
      ? { token: effective.token, chats: effective.chats }
      : null;
  return {
    source: effective.source,
    configured: effective.configured,
    enabled: effective.enabled,
    hasStoredPair,
    chats: effective.chats,
    fingerprint: pair ? chatsFingerprint(pair.token, pair.chats) : null,
    savedAt: row?.updatedAt ?? null,
  };
}

/**
 * Merge an editor submission into what is already stored.
 *
 * An empty token keeps the stored token. The channel list is different: it
 * arrives **complete** on every save (the panel edits it with Add and Delete
 * buttons), so `null` means "the request carried no list at all" — the legacy
 * single-field form — and keeps the stored channels; a list replaces. `clear` is
 * the explicit escape hatch: the operator has to ask for the credentials to be
 * removed, which is the difference between a form mistake and a deliberate step
 * back to the environment fallback.
 */
export function mergeTelegramSave(
  form: {
    botToken: string;
    chats: TelegramChat[] | null;
    enabled: boolean | null;
    clears: boolean;
  },
  current: TelegramRow | null,
): { botToken: string; chats: TelegramChat[]; enabled: boolean; clears: boolean } {
  const enabled = form.enabled ?? (current?.enabled ?? false);
  if (form.clears) return { botToken: "", chats: [], enabled, clears: true };
  return {
    botToken: form.botToken === "" ? (current?.botToken ?? "") : form.botToken,
    chats: form.chats ?? (current?.chats ?? []),
    enabled,
    clears: false,
  };
}

/** A bot token is `digits:40-ish-url-safe`; a chat id is a number, often negative. */
export function telegramCredentialProblem(token: string, chats: TelegramChat[]): string | null {
  if (token === "" && chats.length === 0) return null;
  if (token === "" || chats.length === 0) {
    return "A bot token and at least one chat id are both needed — one without the other cannot deliver.";
  }
  if (!/^\d{6,10}:[A-Za-z0-9_-]{20,}$/.test(token)) {
    return "That does not look like a bot token. Telegram's format is digits, a colon, then the secret.";
  }
  const seen = new Set<string>();
  for (const chat of chats) {
    if (!/^-?\d{4,20}$/.test(chat.chatId)) {
      return "That does not look like a chat id. It is a number — a group id starts with a minus sign.";
    }
    if (seen.has(chat.chatId)) {
      return "Two channels with the same chat id would page you twice. Remove one.";
    }
    seen.add(chat.chatId);
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
  "Intraday Scanner: this is a test message, not an alert.",
  "If you can read this, the bot token and chat id in the dashboard are correct.",
  "Real alerts come from the worker: session high/low, opening range and VWAP breaks.",
].join("\n");
