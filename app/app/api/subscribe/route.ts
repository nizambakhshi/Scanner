import { body, fail, json } from "@/lib/api";
import { subscribeProblem } from "@/lib/accounts";
import { createSubscriptionRequest } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

/**
 * `POST /api/subscribe` — the public queue, no session required.
 *
 * Deliberately thin: validate with the same pure rules the Accounts page uses,
 * hand the row to `createSubscriptionRequest`, and say the same thing whether
 * the address is new or already queued — the queue's partial unique index makes
 * a repeat ask a `DO NOTHING`, not an error, and the applicant does not need to
 * know which half of that happened. Nothing is ever created here: approving a
 * request is a Super Admin action on the Accounts page, which is also where the
 * first password is set and handed over personally — this app sends no mail, so
 * an account someone else could sign straight into would be a liability, not a
 * feature. The client IP rides along only for the log line this route
 * deliberately does not keep (there is no table for it until 0013 exists); the
 * response echoes nothing back but the note.
 */
export async function POST(request: Request) {
  const form = await body(request);
  const input = {
    email: (form.email ?? "").trim().toLowerCase(),
    plan: form.plan ?? "",
    note: (form.note ?? "").trim(),
  };
  const problem = subscribeProblem(input);
  if (problem) return fail(problem, 400);

  const result = await createSubscriptionRequest(input);
  if (!result.ok) return fail(result.error, result.status);
  return json({ ok: true, note: result.note });
}
