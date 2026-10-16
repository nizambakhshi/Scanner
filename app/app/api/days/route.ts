import { body, clientIp, fail, adminGuard, json, read } from "@/lib/api";
import { parseDays } from "@/lib/days";
import { readDaysState, saveDays } from "@/lib/alerts-store";

export const dynamic = "force-dynamic";

/**
 * `GET /api/days` — the exceptions document the worker will read next minute.
 *
 * `readDaysState` folds in "a row exists but this build cannot parse it", in
 * which case `source` is `default`: the plain weekday rule is what runs, and the
 * page says so rather than showing an unobeyable calendar.
 */
export async function GET() {
  const { denied } = await adminGuard();
  if (denied) return denied;
  return read(() => readDaysState());
}

/**
 * `POST /api/days` — body: `off_days` and `on_days`, each a JSON string array of
 * `{date, note}`. The lists arrive **complete** on every save, the same contract
 * the Telegram channel list uses: the page edits the list it shows.
 *
 * Everything validation-wise lives in `parseDays` (real calendar dates, sorted,
 * deduped, no date in both lists), so the page and the worker's gate can never
 * disagree about which days are on. The write is one `ops_settings` upsert plus
 * an `audit_logs` row carrying the date-level diff; the running worker re-reads
 * the key within 60 s and the gate applies at the next session-open decision.
 */
export async function POST(request: Request) {
  const { user, denied } = await adminGuard();
  if (denied) return denied;
  const form = await body(request);
  // `body()` stringifies every field, so a JSON array travels as a JSON *string*.
  const document = {
    off_days: parseList(form.off_days, "off_days"),
    on_days: parseList(form.on_days, "on_days"),
  };
  const bad = [document.off_days, document.on_days].find((entry) => typeof entry === "string");
  if (typeof bad === "string") return fail(bad, 400);
  const parsed = parseDays(document);
  if (!parsed.ok) return fail(parsed.error, 400);
  try {
    const result = await saveDays({
      doc: parsed.doc,
      actor: user.email,
      actorRole: user.role,
      requestIp: clientIp(request),
    });
    return json({
      ok: true,
      doc: parsed.doc,
      changes: result.changes,
      applied: result.applied,
      note: result.applied
        ? "Saved. The scanner gate obeys this from the next session open — no restart."
        : "Nothing changed — the saved calendar already says this.",
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), 500);
  }
}

/** A JSON-stringified `{date, note}` list, or the sentence explaining why it is not one. */
function parseList(raw: string | undefined, field: string): unknown[] | string {
  const value = (raw ?? "").trim();
  if (value === "") return []; // an absent list means "no exceptions of that kind"
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return `${field} must be a JSON list of {date, note} entries.`;
  }
  if (!Array.isArray(parsed)) return `${field} must be a JSON list of {date, note} entries.`;
  return parsed;
}
