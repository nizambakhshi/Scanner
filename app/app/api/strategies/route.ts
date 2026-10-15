import { body, clientIp, fail, guard, json, read } from "@/lib/api";
import { parseStrategyEdits } from "@/lib/alert-strategies";
import { readStrategiesState, saveStrategies } from "@/lib/alerts-store";

export const dynamic = "force-dynamic";

/**
 * `GET /api/strategies` — the document the worker will obey next minute.
 *
 * `readStrategiesState` already folds in "the saved row is unreadable by this
 * build", so a client never has to guess whether `doc` came from the database or
 * from the shipped defaults: `source` says which and `problem` says why.
 */
export async function GET() {
  const { denied } = await guard();
  if (denied) return denied;
  return read(() => readStrategiesState());
}

/**
 * `POST /api/strategies` — body: dotted fields, `orb.threshold_pct`, `vwap.enabled`, …
 *
 * Validated against the document already in force, with the worker's own ranges
 * (`lib/alert-strategies.ts` mirrors `StrategyParams.problems`), so a value the
 * detector would reject never reaches the row. A partial submission keeps the
 * fields it does not mention rather than resetting them.
 *
 * The write is an upsert of `ops_settings['alert_strategies']` plus an
 * `audit_logs` row carrying the field-level diff, in one statement. The running
 * worker re-reads the key within 60 s — there is nothing to restart, and the
 * response says so because an operator who believes a save needs a deploy will
 * go and do one.
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;
  const form = await body(request);
  const current = await readStrategiesState();
  const parsed = parseStrategyEdits(form, current.doc);
  if (!parsed.ok) return fail(parsed.error, 400);
  try {
    const result = await saveStrategies({
      doc: parsed.doc,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    return json({
      ok: true,
      changes: result.changes,
      warnings: [...parsed.warnings, ...result.warnings],
      applied: Object.keys(result.changes).length > 0,
      note:
        Object.keys(result.changes).length > 0
          ? "Saved. The worker picks this up within a minute; no restart."
          : "Nothing changed — the saved document already says this.",
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), 500);
  }
}
