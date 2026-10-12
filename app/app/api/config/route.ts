import { body, clientIp, fail, guard, json } from "@/lib/api";
import { parseConfigEdits, saveScannerConfig } from "@/lib/scanner-config";

export const dynamic = "force-dynamic";

/**
 * `POST /api/config` — body: the seven qualification/watchlist numbers.
 *
 * Inserts a NEW versioned `scanner_configs` row (unchanged columns copied from
 * the active row) and journals the old→new diff into `audit_logs` in the same
 * statement. Signing in is the gate: every account on this deployment is an
 * operator, and there is no anonymous path to a config write.
 *
 * The worker reads this table once, when it opens its next session — so a save
 * changes what the NEXT session qualifies on. The running session keeps the
 * values it started with; the config page says so next to the form.
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;
  const form = await body(request);
  const parsed = parseConfigEdits(form);
  if (!parsed.ok) return fail(parsed.error, 400);
  try {
    const result = await saveScannerConfig({
      values: parsed.values,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    return json(result);
  } catch (error) {
    // "No row to edit" is operator-visible state (400); a database failure is a
    // server problem (500). The message is already redacted — no DSN can appear.
    const message = error instanceof Error ? error.message : String(error);
    const status = message.startsWith("no scanner_configs row") ? 400 : 500;
    return fail(message.slice(0, 200), status);
  }
}
