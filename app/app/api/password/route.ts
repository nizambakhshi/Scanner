import { changePasswordProblem } from "@/lib/auth/password";
import { currentSessionId } from "@/lib/auth/session";
import { changePassword } from "@/lib/auth/users";
import { body, clientIp, fail, guard, json } from "@/lib/api";

export const dynamic = "force-dynamic";

/**
 * `POST /api/password` — the signed-in operator changes their own password.
 *
 * There is no reset link: this deployment has no email transport, and an
 * unauthenticated endpoint that accepts an address and mails a token would be the
 * one thing an attacker could reach from outside. The two supported paths are
 * therefore (1) change it here while signed in, which also signs out every other
 * session, and (2) `npm run reset-password` on the machine holding the database,
 * for when the password itself is what is lost.
 *
 * The guard is the whole point: without a valid session cookie this is a 401, and
 * the endpoint cannot be used to probe addresses. A wrong *current* password is
 * throttled exactly like a failed sign-in (`login_attempts`, 8 per 15 minutes).
 */
export async function POST(request: Request) {
  const { user, denied } = await guard();
  if (denied) return denied;

  const form = await body(request);
  const current = form.currentPassword ?? "";
  const next = form.newPassword ?? "";

  // Confirming is checked here rather than in the pure helper, because "Retype"
  // is a property of the form, not of a password policy.
  const problem = changePasswordProblem(current, next);
  if (problem) return fail(problem, 400);
  if (form.confirmPassword !== undefined && form.confirmPassword !== next) {
    return fail("The two new passwords do not match.", 400);
  }

  const result = await changePassword(user, current, next, {
    ip: clientIp(request),
    // Keep *this* session alive so the operator is not logged out by their own save.
    keepSessionId: await currentSessionId(),
  });
  if (!result.ok) return fail(result.error, result.status);
  return json({ ok: true, email: result.email, revokedSessions: result.revokedSessions });
}
