import { body, clientIp, fail, adminGuard, json, read } from "@/lib/api";
import { readTelegramRow, readTelegramState, saveTelegram } from "@/lib/alerts-store";
import { mergeTelegramSave, telegramCredentialProblem, type TelegramChat } from "@/lib/telegram";

export const dynamic = "force-dynamic";

/**
 * `GET /api/telegram` — the credential *state*, never the token.
 *
 * `{configured, enabled, hasStoredPair, chats, fingerprint, savedAt, source,
 * lastChange}` and nothing else. The channel list is deliberately visible —
 * the page has to let the operator delete a channel they can see — while the
 * bot token exists only in the database row and in this process's memory for as
 * long as it takes to send one message. `fingerprint` lets the page be compared
 * against the worker's log line without either one revealing anything.
 */
export async function GET() {
  const { denied } = await adminGuard();
  if (denied) return denied;
  return read(() => readTelegramState());
}

/**
 * `POST /api/telegram` — body: `bot_token`, `chats` (JSON array of
 * `{chat_id, label}`, sent complete on every save), `enabled`, optional `clear`.
 *
 * A blank token keeps what is stored, because a form that has to be re-typed on
 * every save is a form that quietly wipes the credentials. The channel list has
 * no merge: an editor that added and deleted rows posts the list it shows, and a
 * request that carries no list at all (the legacy single-`chat_id` form) keeps
 * or replaces the stored one as before.
 *
 * Every half is checked for shape before anything is written. A token that
 * cannot be a token is far better caught here than as a silent 401 inside the
 * worker's delivery queue, where the only symptom would be alerts that never
 * arrive.
 *
 * The response is `saveTelegram`'s masked view, so the caller can re-render
 * without a second read. `enabled: false` with a complete set is reported as the
 * state it is: credentials kept, pushing stopped.
 */
export async function POST(request: Request) {
  const { user, denied } = await adminGuard();
  if (denied) return denied;
  const form = await body(request);
  const current = await readTelegramRow();
  const merged = mergeTelegramSave(parseTelegramForm(form), current);
  const problem = telegramCredentialProblem(merged.botToken, merged.chats);
  if (problem) return fail(problem, 400);
  if (merged.botToken === "" && merged.chats.length === 0 && !merged.clears) {
    return fail("Nothing to save: no token, no chat id, and no credentials stored yet.", 400);
  }
  try {
    const saved = await saveTelegram({
      enabled: merged.enabled,
      botToken: merged.botToken,
      chats: merged.chats,
      cleared: merged.clears,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    const note = saved.changed
      ? merged.clears
        ? "Credentials cleared from the database. The worker falls back to its own environment, if it has one."
        : saved.configured
          ? `Saved across ${merged.chats.length} channel${merged.chats.length === 1 ? "" : "s"}. The worker applies the new pair within a minute — press Send test to prove it reaches you.`
          : "Saved with Telegram switched off: the credentials stay in the database and the worker will not push."
      : "Nothing changed — the stored channels and the switch already say this.";
    return json({ ok: true, state: saved, note });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), 500);
  }
}

/**
 * Turn the flat `body()` strings into `mergeTelegramSave`'s structured input.
 *
 * `chats` arrives as a JSON string field — `body()` stringifies anything else,
 * so an array literal would reach the parser as `"[object Object]"`. A request
 * without a list but with a `chat_id` is the legacy single-channel form: it
 * replaces the stored channels with that one id.
 */
function parseTelegramForm(form: Record<string, string>): {
  botToken: string;
  chats: TelegramChat[] | null;
  enabled: boolean | null;
  clears: boolean;
} {
  const clears = /^(1|true|yes|on)$/i.test((form.clear ?? "").trim());
  const botToken = (form.bot_token ?? "").trim();
  const enabledRaw = form.enabled;
  const enabled =
    enabledRaw === undefined ? null : /^(1|true|yes|on)$/i.test(String(enabledRaw).trim());
  let chats: TelegramChat[] | null = null;
  const raw = (form.chats ?? "").trim();
  if (raw !== "") {
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (!Array.isArray(parsed)) return { botToken, chats: [], enabled, clears };
      chats = parsed.map((entry) => {
        const chat = (entry ?? {}) as { chat_id?: unknown; label?: unknown };
        return {
          chatId: typeof chat.chat_id === "string" ? chat.chat_id.trim() : "",
          label: typeof chat.label === "string" && chat.label.trim() !== "" ? chat.label.trim() : null,
        };
      });
    } catch {
      // A malformed list is an empty list: it fails validation below rather than
      // silently keeping a set the operator was trying to edit.
      chats = [];
    }
    return { botToken, chats, enabled, clears };
  }
  const legacy = (form.chat_id ?? "").trim();
  if (legacy !== "") chats = [{ chatId: legacy, label: null }];
  return { botToken, chats, enabled, clears };
}
