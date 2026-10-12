import { guard, read } from "@/lib/api";
import { getRetentionState } from "@/lib/ops";

export const dynamic = "force-dynamic";

/**
 * `GET /api/ops/retention`
 *
 * The saved ageing window and the newest purge audit row — what the retention
 * panel renders before it saves anything.
 */
export async function GET() {
  const { denied } = await guard();
  if (denied) return denied;
  return read(() => getRetentionState());
}
