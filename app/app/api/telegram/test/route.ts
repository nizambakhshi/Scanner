import { clientIp, fail, guard, json } from "@/lib/api";
import { readTelegramRow, recordTelegramTest } from "@/lib/alerts-store";
import { TEST_MESSAGE, credentialFingerprint, effectiveTelegram, sendTelegramMessage } from "@/lib/telegram";

export const dynamic = "force-dynamic";

/**
 * `POST /api/telegram/test` — send one message, say what happened.
 *
 * This is the whole reason the dashboard holds a Telegram credential: an operator
 * who saves a bot token must be able to prove it reaches them *now*, rather than
 * waiting for a breakout at 11:40 to find out the chat id was one digit short.
 * Real alerts are still raised, deduped and delivered by the worker — the page
 * sends nothing else, so there is exactly one sender of record and one cooldown
 * table.
 *
 * It sends to the *effective* pair: the database row if it has a complete one that
 * is switched on, otherwise the environment the worker would fall back to. That is
 * the same resolution `lib/telegram.ts` documents and the same one the page's
 * status line reports, so a green test means the configuration the page is
 * showing works.
 *
 * Nothing secret comes back out. `{ok, detail, source, fingerprint}` — and the
 * attempt is journalled in the same shape.
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;
  const row = await readTelegramRow();
  const effective = effectiveTelegram(row);
  if (!effective.configured || effective.token === "" || effective.chatId === "") {
    return fail(
      row?.enabled === false
        ? "Telegram is switched off on this page. Turn it on, then press Send test."
        : "No bot token and chat id to send with. Save them above, or set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for this app.",
      400,
    );
  }
  const result = await sendTelegramMessage(effective.token, effective.chatId, TEST_MESSAGE);
  const fingerprint = credentialFingerprint(effective.token, effective.chatId);
  try {
    await recordTelegramTest({
      ok: result.ok,
      detail: result.detail,
      source: effective.source,
      fingerprint,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
  } catch {
    // A journal that will not take the entry must not be reported as a failed
    // send: the operator's phone is the ground truth for whether it worked.
  }
  return json({ ok: result.ok, detail: result.detail, source: effective.source, fingerprint });
}
