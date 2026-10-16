import { clientIp, fail, adminGuard, json } from "@/lib/api";
import { readTelegramRow, recordTelegramTest } from "@/lib/alerts-store";
import { TEST_MESSAGE, chatsFingerprint, effectiveTelegram, sendTelegramMessage } from "@/lib/telegram";

export const dynamic = "force-dynamic";

/**
 * `POST /api/telegram/test` — send one message to every channel, say what happened.
 *
 * This is the whole reason the dashboard holds a Telegram credential: an operator
 * who saves a bot token must be able to prove it reaches them *now*, rather than
 * waiting for a breakout at 11:40 to find out the chat id was one digit short.
 * Real alerts are still raised, deduped and delivered by the worker — the page
 * sends nothing else, so there is exactly one sender of record and one cooldown
 * table.
 *
 * It sends to every *effective* channel: the database row if it has a token and
 * channels and is switched on, otherwise the environment the worker would fall
 * back to. A channel the bot cannot see fails on its own line and does not hide
 * the others — "3 of 4 delivered" is the sentence the operator needs, not a
 * single yes/no that would call a working setup broken because one group was
 * archived.
 *
 * Nothing secret comes back out. `{ok, delivered, total, results[], detail,
 * source, fingerprint}` — and the attempt is journalled as one row in the same
 * shape. The per-channel results carry the chat id (the operator can see the
 * list on the page anyway) and a token-free detail sentence.
 */
export async function POST(request: Request) {
  const { user, denied } = await adminGuard();
  if (denied) return denied;
  const row = await readTelegramRow();
  const effective = effectiveTelegram(row);
  if (!effective.configured || effective.token === "" || effective.chats.length === 0) {
    return fail(
      row?.enabled === false
        ? "Telegram is switched off on this page. Turn it on, then press Send test."
        : "No bot token and chat id to send with. Save them above, or set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for this app.",
      400,
    );
  }
  const results = [];
  for (const chat of effective.chats) {
    const sent = await sendTelegramMessage(effective.token, chat.chatId, TEST_MESSAGE);
    results.push({ chat_id: chat.chatId, label: chat.label, ok: sent.ok, detail: sent.detail });
  }
  const delivered = results.filter((result) => result.ok).length;
  const ok = delivered === results.length;
  const detail = ok
    ? delivered === 1
      ? results[0].detail
      : `Telegram accepted the message on all ${delivered} channels.`
    : `${delivered} of ${results.length} channels received it. ${results
        .filter((result) => !result.ok)
        .map((result) => `${result.label ?? result.chat_id}: ${result.detail}`)
        .join(" · ")}`;
  const fingerprint = chatsFingerprint(effective.token, effective.chats);
  try {
    await recordTelegramTest({
      ok,
      detail,
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
  return json({ ok, delivered, total: results.length, results, detail, source: effective.source, fingerprint });
}
