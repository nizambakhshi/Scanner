import { body, clientIp, fail, guard, json, read } from "@/lib/api";
import { readTelegramRow, readTelegramState, saveTelegram } from "@/lib/alerts-store";
import { mergeTelegramSave, telegramCredentialProblem } from "@/lib/telegram";

export const dynamic = "force-dynamic";

/**
 * `GET /api/telegram` — the credential *state*, never the credentials.
 *
 * `{configured, enabled, hasStoredPair, fingerprint, savedAt, source, lastChange}`
 * and nothing else. The page renders from this; a bot token exists only in the
 * database row and in this process's memory for as long as it takes to send one
 * message.
 */
export async function GET() {
  const { denied } = await guard();
  if (denied) return denied;
  return read(() => readTelegramState());
}

/**
 * `POST /api/telegram` — body: `bot_token`, `chat_id`, `enabled`, optional `clear`.
 *
 * A blank token or chat id keeps what is already stored, because a form that has
 * to be re-typed on every save is a form that quietly wipes the destination.
 * `clear` is the only way to remove them, and it is explicit for the same reason.
 *
 * Both halves are checked for shape before anything is written. A token that
 * cannot be a token is far better caught here than as a silent 401 inside the
 * worker's delivery queue, where the only symptom would be alerts that never
 * arrive.
 *
 * The response is `saveTelegram`'s masked view, so the caller can re-render
 * without a second read. `enabled: false` with a complete pair is reported as the
 * state it is: credentials kept, pushing stopped.
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;
  const form = await body(request);
  const current = await readTelegramRow();
  const merged = mergeTelegramSave(form, current);
  const problem = telegramCredentialProblem(merged.botToken, merged.chatId);
  if (problem) return fail(problem, 400);
  if (merged.botToken === "" && merged.chatId === "" && !merged.clears) {
    return fail("Nothing to save: no token, no chat id, and no credentials stored yet.", 400);
  }
  try {
    const saved = await saveTelegram({
      enabled: merged.enabled,
      botToken: merged.botToken,
      chatId: merged.chatId,
      cleared: merged.clears,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    const note = saved.changed
      ? merged.clears
        ? "Credentials cleared from the database. The worker falls back to its own environment, if it has one."
        : saved.configured
          ? "Saved. The worker applies the new pair within a minute — press Send test to prove it reaches you."
          : "Saved with Telegram switched off: the credentials stay in the database and the worker will not push."
      : "Nothing changed — the stored pair and the switch already say this.";
    return json({ ok: true, state: saved, note });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), 500);
  }
}
