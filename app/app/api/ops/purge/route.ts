import { body, clientIp, fail, guard, json } from "@/lib/api";
import { RETENTION_MAX, RETENTION_MIN, runPurge } from "@/lib/ops";

export const dynamic = "force-dynamic";

/**
 * `POST /api/ops/purge` — body `{ days: number, symbol?: string }`.
 *
 * Deletes candles and indicator snapshots older than `days` (optionally only
 * one instrument's), saves `days` as the retention knob the worker's automatic
 * purge also reads, and journals the run into `audit_logs`. Signing in is the
 * admin gate: every account on this deployment is an operator, and there is no
 * anonymous path to a DELETE.
 *
 * There is no undo by design — the response carries the per-table counts the
 * statement reported, and the audit row records them.
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;
  const form = await body(request);
  const days = Number(form.days);
  if (!Number.isFinite(days) || days < RETENTION_MIN || days > RETENTION_MAX) {
    return fail(`days must be between ${RETENTION_MIN} and ${RETENTION_MAX}.`, 400);
  }
  const symbol = form.symbol?.trim() || null;
  try {
    const result = await runPurge({
      days,
      symbol,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    return json(result);
  } catch (error) {
    // Unknown-symbol and bound errors are operator input problems (400); a
    // database failure is a server problem (500). The message is already
    // operator-facing — no DSN can appear in either.
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), message.startsWith("unknown symbol") || message.startsWith("days must") ? 400 : 500);
  }
}
