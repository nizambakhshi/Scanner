import { body, clientIp, fail, adminGuard, json, read } from "@/lib/api";
import { getRetentionState, saveRetentionWindow } from "@/lib/ops";

export const dynamic = "force-dynamic";

/**
 * `GET /api/ops/retention`
 *
 * The saved ageing window and the newest purge audit row — what the retention
 * panel renders before it saves anything.
 */
export async function GET() {
  const { denied } = await adminGuard();
  if (denied) return denied;
  return read(() => getRetentionState());
}

/**
 * `PUT /api/ops/retention` — body `{ days: number }`.
 *
 * Saves the ageing window and nothing else: no DELETE runs here, which is why
 * this is the only path that accepts `days: 0` (keep forever). The purge POST
 * stays 1..30. Guarded like every ops route, and journalled as `retention_set`
 * so turning ageing off is as auditable as running a purge.
 */
export async function PUT(request: Request) {
  const { user, denied } = await adminGuard();
  if (denied) return denied;
  const form = await body(request);
  try {
    const result = await saveRetentionWindow({
      days: Number(form.days),
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    return json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), message.startsWith("days must") ? 400 : 500);
  }
}
