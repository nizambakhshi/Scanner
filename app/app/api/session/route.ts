import { authenticate } from "@/lib/auth/users";
import { createSession, destroySession, getCurrentUser } from "@/lib/auth/session";
import { authSetupProblem } from "@/lib/auth/users";
import { body, clientIp, fail, guard, json, read } from "@/lib/api";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/** `GET /api/session` — who am I, and which data source is this process reading? */
export async function GET() {
  const { user, denied } = await guard();
  if (denied) return denied;
  const meta = await source().meta();
  return json({ user, meta, setupProblem: authSetupProblem() });
}

/**
 * `POST /api/session` — password login.
 *
 * The response never distinguishes "no such account" from "wrong password", and
 * no failure path returns anything derived from the stored hash. On success the
 * session cookie is set by `createSession`, which also journals the session in
 * `app_sessions` when a database is configured.
 */
export async function POST(request: Request) {
  const form = await body(request);
  const problem = authSetupProblem();
  if (problem) return fail(problem, 503, { setupProblem: true });
  const result = await authenticate(form.email ?? "", form.password ?? "", { ip: clientIp(request) });
  if (!result.ok) return fail(result.error, result.status);
  await createSession(result.user, { ip: clientIp(request), userAgent: request.headers.get("user-agent") });
  return json({ ok: true, user: result.user });
}

/** `DELETE /api/session` — drop this cookie, and (with a database) this row only. */
export async function DELETE() {
  const current = await getCurrentUser();
  await destroySession(current?.id);
  return json({ ok: true });
}

/** Used by the sign-out button on pages, where a form can only POST/GET. */
export async function PATCH() {
  return read(async () => {
    const current = await getCurrentUser();
    await destroySession(current?.id);
    return { ok: true };
  });
}
