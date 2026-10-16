import { body, clientIp, fail, adminGuard, json } from "@/lib/api";
import {
  adminCreateUser,
  adminResetPassword,
  adminSetPlan,
  adminSetActive,
  handleSubscriptionRequest,
} from "@/lib/auth/users";

export const dynamic = "force-dynamic";

/**
 * `POST /api/accounts` — the Super Admin's hand on `app_users`, one action per
 * press. Admin-guarded; a plain user gets the same 403 here as on every other
 * control surface.
 *
 * Body: `action` plus its fields —
 *   `create`          email, password, role, plan, optional display_name
 *   `reset_password`  id, password       (revokes the target's live sessions)
 *   `set_plan`        id, plan           (needs migration 0013's column)
 *   `set_active`      id, active         (disabling also signs the account out)
 *   `handle_request`  id, decision, password (approve creates the account)
 *
 * Passwords are write-only in the truest sense: they are hashed here and never
 * echoed — no response, no audit row, and the audit row carries only which
 * field of which account moved.
 */
export async function POST(request: Request) {
  const { user, denied } = await adminGuard();
  if (denied) return denied;
  const form = await body(request);
  const ip = clientIp(request);
  const actor = user;
  try {
    switch (form.action) {
      case "create": {
        const result = await adminCreateUser(
          actor,
          {
            email: form.email ?? "",
            password: form.password ?? "",
            role: form.role || "USER",
            plan: form.plan || "BASIC",
            displayName: form.display_name,
          },
          ip,
        );
        return result.ok ? json({ ok: true, note: result.note }) : fail(result.error, result.status);
      }
      case "reset_password": {
        const result = await adminResetPassword(actor, form.id ?? "", form.password ?? "", ip);
        return result.ok ? json({ ok: true, note: result.note }) : fail(result.error, result.status);
      }
      case "set_plan": {
        const result = await adminSetPlan(actor, form.id ?? "", form.plan ?? "", ip);
        return result.ok ? json({ ok: true, note: result.note }) : fail(result.error, result.status);
      }
      case "set_active": {
        const result = await adminSetActive(actor, form.id ?? "", /^(1|true|yes|on)$/i.test((form.active ?? "").trim()), ip);
        return result.ok ? json({ ok: true, note: result.note }) : fail(result.error, result.status);
      }
      case "handle_request": {
        const decision = (form.decision ?? "").trim().toUpperCase();
        if (decision !== "APPROVE" && decision !== "DISMISS") {
          return fail("decision must be APPROVE or DISMISS", 400);
        }
        const result = await handleSubscriptionRequest(actor, form.id ?? "", decision, form.password ?? "", ip);
        return result.ok ? json({ ok: true, note: result.note }) : fail(result.error, result.status);
      }
      default:
        return fail(`Unknown action: ${(form.action ?? "").slice(0, 32) || "(none)"}`, 400);
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return fail(message.slice(0, 200), 500);
  }
}
