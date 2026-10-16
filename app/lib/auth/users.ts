/**
 * The user store.
 *
 * One operator today, role-ready tomorrow — the same stance as `app_users`
 * (§16), where `SUPER_ADMIN` exists so a second role can be added without a
 * migration. Two shapes of truth:
 *
 * * **Database**: `app_users` rows with a scrypt hash. The first login attempt
 *   bootstraps the admin from `AUTH_ADMIN_EMAIL` + `AUTH_ADMIN_PASSWORD_HASH` when
 *   the table is empty — the hash comes from the environment, so no plaintext
 *   password is ever stored, shipped, or passed through this process.
 * * **Replay sample**: no user table exists, so the environment's admin is the
 *   only account that can authenticate, verified against the same hash format.
 *
 * Failed-login throttling is `login_attempts` in the database (shared across
 * replicas) and a small in-process window in replay mode. The message is
 * identical either way: it must not reveal whether an address is registered.
 */

import { env, usesPostgres } from "../env";
import { query, type Raw } from "../db";
import { hashPassword, passwordProblem, verifyPassword } from "./password";
import { createAccountProblem, normalisePlan, type Plan } from "../accounts";
import { isAdminRole } from "../roles";

export type SessionUser = {
  id: string;
  email: string;
  role: string;
  displayName: string | null;
  source: "postgres" | "replay";
};

type StoredUser = SessionUser & { passwordHash: string };

const FAILURE_WINDOW_MINUTES = 15;
const MAX_FAILURES = 8;

/** Local throttle used only when there is no database to share state with. */
const localFailures = new Map<string, { count: number; resetAt: number }>();

function key(email: string, ip: string | null): string {
  return `${email.toLowerCase()}|${ip ?? "unknown"}`;
}

function localBlocked(email: string, ip: string | null): boolean {
  const entry = localFailures.get(key(email, ip));
  if (!entry) return false;
  if (entry.resetAt < Date.now()) {
    localFailures.delete(key(email, ip));
    return false;
  }
  return entry.count >= MAX_FAILURES;
}

function localRecordFailure(email: string, ip: string | null) {
  const id = key(email, ip);
  const entry = localFailures.get(id);
  const resetAt = Date.now() + FAILURE_WINDOW_MINUTES * 60_000;
  localFailures.set(id, { count: (entry?.count ?? 0) + 1, resetAt: entry?.resetAt ?? resetAt });
}

async function dbBlocked(email: string, ip: string | null): Promise<boolean> {
  const rows = await query<Raw>(
    `SELECT COUNT(*)::int AS failures
     FROM login_attempts
     WHERE lower(email) = lower($1::text)
       AND succeeded IS FALSE
       AND attempted_at > now() - interval '${FAILURE_WINDOW_MINUTES} minutes'
       AND ($2::text IS NULL OR request_ip IS DISTINCT FROM $2::text OR request_ip = $2::text)`,
    [email, ip],
  );
  // COUNT(*) arrives as int8; `Number` keeps a null row readable as zero.
  return Number(rows[0]?.failures ?? 0) >= MAX_FAILURES;
}

async function dbRecordAttempt(email: string, ip: string | null, succeeded: boolean): Promise<void> {
  await query(
    "INSERT INTO login_attempts (email, request_ip, succeeded) VALUES ($1::text, $2::text, $3::boolean)",
    [email, ip, succeeded],
  );
}

/** Create the admin row from the environment the first time anyone signs in. */
async function bootstrapAdmin(): Promise<void> {
  if (env.adminEmail === "" || env.adminPasswordHash === "") return;
  const rows = await query<Raw>("SELECT COUNT(*)::int AS total FROM app_users");
  if ((rows[0]?.total as number | undefined) ?? 0 > 0) return;
  await query(
    `INSERT INTO app_users (email, password_hash, role, display_name)
     VALUES ($1::text, $2::text, 'SUPER_ADMIN', $3::text)
     ON CONFLICT (email) DO NOTHING`,
    [env.adminEmail, env.adminPasswordHash, "Scanner operator"],
  );
}

async function findStoredUser(email: string): Promise<StoredUser | null> {
  if (usesPostgres()) {
    await bootstrapAdmin();
    const rows = await query<Raw>(
      `SELECT id::text AS id, email, role, display_name, password_hash, is_active
       FROM app_users WHERE lower(email) = lower($1::text)`,
      [email],
    );
    const row = rows[0];
    if (!row || row.is_active === false) return null;
    return {
      id: String(row.id),
      email: String(row.email),
      role: String(row.role ?? "SUPER_ADMIN"),
      displayName: row.display_name === null ? null : String(row.display_name),
      source: "postgres",
      passwordHash: String(row.password_hash),
    };
  }
  // Replay mode: the environment admin, verified against its stored hash.
  if (env.adminEmail === "" || env.adminPasswordHash === "") return null;
  if (email.toLowerCase() !== env.adminEmail) return null;
  return {
    id: env.adminEmail,
    email: env.adminEmail,
    role: "SUPER_ADMIN",
    displayName: "Scanner operator",
    source: "replay",
    passwordHash: env.adminPasswordHash,
  };
}

export type AuthResult = { ok: true; user: SessionUser } | { ok: false; status: number; error: string };

const GENERIC_FAILURE = "That email or password did not match.";

export async function authenticate(
  email: string,
  password: string,
  request: { ip: string | null },
): Promise<AuthResult> {
  const normalised = email.trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(normalised)) {
    return { ok: false, status: 400, error: "Enter the email address for this dashboard." };
  }
  if (password.length === 0) return { ok: false, status: 400, error: "Enter your password." };

  const blocked = usesPostgres() ? await dbBlocked(normalised, request.ip) : localBlocked(normalised, request.ip);
  if (blocked) {
    return {
      ok: false,
      status: 429,
      error: `Too many failed attempts. Try again in ${FAILURE_WINDOW_MINUTES} minutes.`,
    };
  }

  const user = await findStoredUser(normalised);
  // A missing account and a wrong password take the same path and return the same
  // words, so the form cannot be used to enumerate addresses.
  if (!user || !(await verifyPassword(password, user.passwordHash))) {
    if (usesPostgres()) await dbRecordAttempt(normalised, request.ip, false);
    else localRecordFailure(normalised, request.ip);
    return { ok: false, status: 401, error: GENERIC_FAILURE };
  }

  if (usesPostgres()) {
    // The password is proved at this point. Bookkeeping after it must not turn a
    // correct login into a failure when the session pooler is at its cap — the
    // audit read paths already fail open, and a refused login_attempts write is
    // exactly the EMAXCONNSESSION storm a reload burst creates.
    await dbRecordAttempt(normalised, request.ip, true).catch(() => {});
    await query("UPDATE app_users SET last_login_at = now() WHERE id = $1::bigint", [
      user.id,
    ]).catch(() => {});
  } else {
    localFailures.delete(key(normalised, request.ip));
  }
  const { passwordHash: _hash, ...sessionUser } = user;
  return { ok: true, user: sessionUser };
}

export type ChangePasswordResult = { ok: true; email: string; revokedSessions: number } | { ok: false; status: number; error: string };

const WRONG_CURRENT = "That current password did not match.";

/**
 * Change the password of the signed-in operator.
 *
 * Deliberately paranoid, in this order:
 *
 * 1. **The same throttle as sign-in** guards this endpoint too. Someone who has
 *    walk-up access to a signed-out browser can try passwords here exactly as
 *    often as at `/login` — no more — and the attempt is written to
 *    `login_attempts`, so §16's audit list shows a change attempt next to the
 *    logins around it rather than hiding it.
 * 2. **The current password is required**, because a stolen session cookie must
 *    not be enough to take the account over quietly.
 * 3. **Every other session is revoked** once the hash is written. A password
 *    change is also the operator's "someone was in my account, get them out"
 *    button; the request's own session is kept so the form does not log them out
 *    mid-save.
 *
 * Replay mode (no `DATABASE_URL`) has no user table to write to, so it says so
 * rather than accepting a change that would vanish on the next request.
 */
export async function changePassword(
  user: SessionUser,
  currentPassword: string,
  newPassword: string,
  request: { ip: string | null; keepSessionId: string | null },
): Promise<ChangePasswordResult> {
  if (!usesPostgres()) {
    return {
      ok: false,
      status: 409,
      error: "This copy of the dashboard has no database, so a new password could not be stored. Set AUTH_ADMIN_PASSWORD_HASH instead — `npm run hash-password` prints one.",
    };
  }

  // Read the row by id, not by email: the session is the thing being proved.
  const rows = await query<Raw>(
    "SELECT email, password_hash, is_active FROM app_users WHERE id = $1::bigint",
    [user.id],
  );
  const row = rows[0];
  if (!row || row.is_active === false) {
    return { ok: false, status: 403, error: "This account no longer exists or is disabled." };
  }
  const email = String(row.email);

  if (await dbBlocked(email, request.ip)) {
    return {
      ok: false,
      status: 429,
      error: `Too many failed attempts. Try again in ${FAILURE_WINDOW_MINUTES} minutes.`,
    };
  }

  if (!(await verifyPassword(currentPassword, String(row.password_hash)))) {
    await dbRecordAttempt(email, request.ip, false);
    return { ok: false, status: 401, error: WRONG_CURRENT };
  }

  const stored = await hashPassword(newPassword);
  await query("UPDATE app_users SET password_hash = $2::text WHERE id = $1::bigint", [user.id, stored]);

  // Sign-in throttles on failed attempts only, so a successful change needs no
  // housekeeping there — but it is worth an audit row.
  await dbRecordAttempt(email, request.ip, true).catch(() => {});

  const revoked = await query<Raw>(
    `UPDATE app_sessions SET revoked_at = now()
     WHERE user_id = $1::bigint AND revoked_at IS NULL AND id <> COALESCE($2::text, '')
     RETURNING id`,
    [user.id, request.keepSessionId],
  ).catch(() => [{ count: 0 }] as Raw[]);

  return { ok: true, email, revokedSessions: revoked.length };
}

export type AccountFacts = {
  email: string;
  role: string;
  displayName: string | null;
  /** When this account last signed in successfully. */
  lastLoginAt: string | null;
  /** Sessions that are neither revoked nor expired, including this one. */
  liveSessions: number | null;
  canChangePassword: boolean;
};

/** What the account page shows. Every field has a replay-mode answer, so the
 *  page never has to know which mode it is in. */
export async function accountFacts(user: SessionUser): Promise<AccountFacts> {
  const base: AccountFacts = {
    email: user.email,
    role: user.role,
    displayName: user.displayName,
    lastLoginAt: null,
    liveSessions: null,
    canChangePassword: usesPostgres(),
  };
  if (!usesPostgres()) return base;
  const rows = await query<Raw>(
    `SELECT u.last_login_at::text AS last_login_at,
            (SELECT COUNT(*)::int FROM app_sessions s
              WHERE s.user_id = u.id AND s.revoked_at IS NULL AND s.expires_at > now()) AS live_sessions
     FROM app_users u WHERE u.id = $1::bigint`,
    [user.id],
  ).catch(() => [] as Raw[]);
  const row = rows[0];
  if (!row) return base;
  // Postgres prints `2026-10-14 18:30:00.123456+05:30`; `lib/format`'s helpers and
  // `new Date()` both want the `T` separator, and the offset is already correct.
  const lastLoginAt = row.last_login_at === null ? null : String(row.last_login_at).replace(" ", "T");
  return {
    ...base,
    lastLoginAt,
    liveSessions: Number(row.live_sessions ?? 0),
  };
}

/** Why sign-in cannot work at all, in operator terms — variable names, never values. */
export function authSetupProblem(): string | null {
  if (env.sessionSecret.length < 32) return "AUTH_SESSION_SECRET is not set (needs 32+ characters).";
  if (env.adminEmail === "" || env.adminPasswordHash === "") {
    return usesPostgres()
      ? null // an existing app_users table can hold accounts without any env admin
      : "AUTH_ADMIN_EMAIL and AUTH_ADMIN_PASSWORD_HASH are not set — run `npm run hash-password`.";
  }
  return null;
}

/* ── Account management — the Super Admin's half of app_users ─────────────── */

/**
 * Everything below is called only behind `adminGuard` / `requireAdminPage`.
 *
 * The plan column arrives with migration 0013, which is drafted but not yet
 * approved for application — so every read here is *defensive*: a database
 * without the column behaves exactly as the migration's grandfathering would,
 * UNLIMITED for all. That is stated in the row mapper, not hidden in a catch.
 */

export type ManagedUser = {
  id: string;
  email: string;
  role: string;
  displayName: string | null;
  isActive: boolean;
  plan: Plan;
  lastLoginAt: string | null;
  createdAt: string;
};

/** Does this database have the 0013 `plan` column yet? One cheap catalog read. */
export async function planColumnExists(): Promise<boolean> {
  if (!usesPostgres()) return false;
  const rows = await query<Raw>(
    "SELECT 1 FROM information_schema.columns WHERE table_name = 'app_users' AND column_name = 'plan'",
  ).catch(() => [] as Raw[]);
  return rows.length > 0;
}

/**
 * The signed-in user's plan, for the watchlist read paths.
 *
 * Three deliberate answers that are not BASIC: no column (0013 unapplied),
 * replay mode, and a failed read all return UNLIMITED. The first two are 0013's
 * own grandfather rule — nobody is paywalled by a migration that has not been
 * applied — and the third follows the house pattern that read failures fail
 * open: a momentarily unreachable pooler must not blank a paying customer's
 * own watchlist. The cap is a display entitlement, not a security boundary, so
 * the leak on a bad read is a few extra rows for one page load.
 */
export async function planForUser(user: SessionUser): Promise<Plan | "UNKNOWN"> {
  if (!usesPostgres()) return "UNLIMITED";
  // The operator runs the house, they do not rent a room in it: bootstrapAdmin
  // inserts no plan, so the column default would otherwise cap the admin's own
  // watchlist at three.
  if (isAdminRole(user.role)) return "UNLIMITED";
  if (!(await planColumnExists())) return "UNLIMITED";
  const rows = await query<Raw>("SELECT plan FROM app_users WHERE id = $1::bigint", [user.id]).catch(() => [] as Raw[]);
  const plan = rows[0]?.plan;
  return plan === "BASIC" || plan === "UNLIMITED" ? plan : "UNLIMITED";
}

export async function listUsers(): Promise<ManagedUser[]> {
  const hasPlan = await planColumnExists();
  const rows = await query<Raw>(
    `SELECT id::text AS id, email, role, display_name, is_active,
            ${hasPlan ? "plan" : "NULL"}::text AS plan,
            last_login_at::text AS last_login_at, created_at::text AS created_at
     FROM app_users
     ORDER BY CASE role WHEN 'SUPER_ADMIN' THEN 0 WHEN 'ADMIN' THEN 1 ELSE 2 END, email`,
  );
  return rows.map((row) => ({
    id: String(row.id),
    email: String(row.email),
    role: String(row.role),
    displayName: row.display_name === null ? null : String(row.display_name),
    isActive: row.is_active !== false,
    // No column yet → everyone is UNLIMITED, which is what migration 0013's
    // grandfather UPDATE will write for exactly these rows.
    plan: row.plan === null || row.plan === undefined ? "UNLIMITED" : normalisePlan(row.plan),
    lastLoginAt: row.last_login_at === null ? null : String(row.last_login_at).replace(" ", "T"),
    createdAt: String(row.created_at ?? "").replace(" ", "T"),
  }));
}

type AdminResult = { ok: true; note: string } | { ok: false; status: number; error: string };

async function journal(actor: SessionUser, action: string, entityId: string, newValue: unknown, ip: string | null): Promise<void> {
  await query(
    `INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
     VALUES ($1::text, $2::text, $3::text, 'app_users', $4::text, $5::jsonb, $6::text)`,
    [actor.email, actor.role, action, entityId, newValue, ip],
  ).catch(() => {}); // the account write already landed; a journal failure must not undo it silently *or* loudly
}

/**
 * Create an account with a password the admin just set.
 *
 * There is no email sender in this deployment (the account page documents the
 * same limit for password reset), so the admin *has* to hand the password to
 * the person out-of-band. That is the honest flow; a "welcome mail" that goes
 * nowhere would be a lie with a nicer subject line.
 */
export async function adminCreateUser(
  actor: SessionUser,
  input: { email: string; password: string; role: string; plan: string; displayName?: string },
  ip: string | null,
): Promise<AdminResult> {
  const problem = createAccountProblem(input);
  if (problem) return { ok: false, status: 400, error: problem };
  const email = input.email.trim().toLowerCase();
  const pwProblem = passwordProblemForCreate(input.password);
  if (pwProblem) return { ok: false, status: 400, error: pwProblem };
  const stored = await hashPassword(input.password);
  const hasPlan = await planColumnExists();
  const written = await query<Raw>(
    `INSERT INTO app_users (email, password_hash, role, display_name${hasPlan ? ", plan" : ""})
     VALUES ($1::text, $2::text, $3::text, $4::text${hasPlan ? ", $5::text" : ""})
     RETURNING id::text AS id`,
    hasPlan ? [email, stored, input.role, input.displayName?.trim() || null, normalisePlan(input.plan)] : [email, stored, input.role, input.displayName?.trim() || null],
  ).catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    if (/unique|duplicate/i.test(message)) return null;
    throw error;
  });
  if (!written?.[0]) return { ok: false, status: 409, error: "An account with that email already exists." };
  await journal(actor, "account_create", email, { role: input.role, plan: normalisePlan(input.plan) }, ip);
  return { ok: true, note: `Created ${email} as ${input.role}, plan ${normalisePlan(input.plan)}. Hand the password over yourself — this app sends no email.` };
}

/** The create-path password rule: the same strength list the change form uses. */
function passwordProblemForCreate(password: string): string | null {
  if (password.length === 0) return "Set a password for this account.";
  return passwordProblem(password);
}

/**
 * Reset any account's password, from the admin's session.
 *
 * The deliberate differences from the self-service change: no current password
 * (the admin *is* the proof), and **every** session of the target is revoked —
 * including theirs — because a reset they did not ask for must take effect
 * immediately, not at the next cookie expiry. The target then signs in with the
 * new password the admin holds.
 */
export async function adminResetPassword(
  actor: SessionUser,
  targetId: string,
  newPassword: string,
  ip: string | null,
): Promise<AdminResult> {
  const pwProblem = passwordProblemForCreate(newPassword);
  if (pwProblem) return { ok: false, status: 400, error: pwProblem };
  const target = await query<Raw>("SELECT email FROM app_users WHERE id = $1::bigint", [targetId]);
  const row = target[0];
  if (!row) return { ok: false, status: 404, error: "No such account." };
  const stored = await hashPassword(newPassword);
  await query("UPDATE app_users SET password_hash = $2::text WHERE id = $1::bigint", [targetId, stored]);
  const revoked = await query<Raw>(
    "UPDATE app_sessions SET revoked_at = now() WHERE user_id = $1::bigint AND revoked_at IS NULL RETURNING id",
    [targetId],
  ).catch(() => [] as Raw[]);
  await journal(actor, "account_password_reset", String(row.email), { revoked_sessions: revoked.length }, ip);
  return { ok: true, note: `Password reset for ${row.email}; ${revoked.length} live session(s) signed out.` };
}

export async function adminSetPlan(actor: SessionUser, targetId: string, plan: string, ip: string | null): Promise<AdminResult> {
  if (!isPlanValue(plan)) return { ok: false, status: 400, error: "That plan does not exist — BASIC or UNLIMITED." };
  if (!(await planColumnExists())) {
    return { ok: false, status: 409, error: "This database has no plan column yet — apply migration 0013 first. Until then every account is UNLIMITED." };
  }
  const rows = await query<Raw>(
    "UPDATE app_users SET plan = $2::text WHERE id = $1::bigint RETURNING email",
    [targetId, normalisePlan(plan)],
  );
  const row = rows[0];
  if (!row) return { ok: false, status: 404, error: "No such account." };
  await journal(actor, "account_plan_change", String(row.email), { plan: normalisePlan(plan) }, ip);
  return { ok: true, note: `${row.email} is now ${normalisePlan(plan)}. It applies on their next watchlist load.` };
}

export async function adminSetActive(
  actor: SessionUser,
  targetId: string,
  active: boolean,
  ip: string | null,
): Promise<AdminResult> {
  if (targetId === actor.id && !active) {
    return { ok: false, status: 400, error: "You cannot disable the account you are signed in with — that would strand the dashboard." };
  }
  const rows = await query<Raw>(
    "UPDATE app_users SET is_active = $2::boolean WHERE id = $1::bigint RETURNING email",
    [targetId, active],
  );
  const row = rows[0];
  if (!row) return { ok: false, status: 404, error: "No such account." };
  if (!active) {
    await query("UPDATE app_sessions SET revoked_at = now() WHERE user_id = $1::bigint AND revoked_at IS NULL", [targetId]).catch(() => {});
  }
  await journal(actor, active ? "account_enabled" : "account_disabled", String(row.email), { active }, ip);
  return { ok: true, note: active ? `${row.email} can sign in again.` : `${row.email} is disabled and signed out of every session.` };
}

function isPlanValue(value: unknown): boolean {
  return value === "BASIC" || value === "UNLIMITED";
}

/* ── The subscription queue — migration 0013's second table ───────────────── */

export type SubscriptionRequest = {
  id: string;
  email: string;
  planRequested: Plan;
  note: string | null;
  status: "PENDING" | "APPROVED" | "DISMISSED";
  createdAt: string;
  handledAt: string | null;
  handledByEmail: string | null;
};

export async function subscriptionTableExists(): Promise<boolean> {
  if (!usesPostgres()) return false;
  const rows = await query<Raw>(
    "SELECT 1 FROM information_schema.tables WHERE table_name = 'subscription_requests'",
  ).catch(() => [] as Raw[]);
  return rows.length > 0;
}

/** The Subscribe page's queue read, newest first, capped — an admin page, not a search engine. */
export async function listSubscriptionRequests(limit = 50): Promise<SubscriptionRequest[]> {
  if (!(await subscriptionTableExists())) return [];
  const rows = await query<Raw>(
    `SELECT id::text AS id, email, plan_requested, note, status,
            created_at::text AS created_at, handled_at::text AS handled_at, handled_by_email
     FROM subscription_requests
     ORDER BY (status = 'PENDING') DESC, created_at DESC
     LIMIT $1::int`,
    [limit],
  );
  return rows.map((row) => ({
    id: String(row.id),
    email: String(row.email),
    planRequested: normalisePlan(row.plan_requested),
    note: row.note === null ? null : String(row.note),
    status: String(row.status) as SubscriptionRequest["status"],
    createdAt: String(row.created_at ?? "").replace(" ", "T"),
    handledAt: row.handled_at === null ? null : String(row.handled_at).replace(" ", "T"),
    handledByEmail: row.handled_by_email === null ? null : String(row.handled_by_email),
  }));
}

/**
 * A public Subscribe press. Unauthenticated by design — this is the one DB write
 * any visitor makes — so the guards are the shape checks in `lib/accounts.ts`,
 * the length caps, and the partial unique index that keeps one PENDING per
 * address. A duplicate ask answers "already in the queue", which is true and
 * leaks nothing about whether the address exists as an *account*.
 */
export async function createSubscriptionRequest(input: {
  email: string;
  plan: string;
  note: string;
}): Promise<AdminResult> {
  if (!(await subscriptionTableExists())) {
    return { ok: false, status: 409, error: "Subscriptions are not open on this database yet (migration 0013 is not applied)." };
  }
  const email = input.email.trim().toLowerCase();
  // The conflict target mirrors the partial unique index exactly, so a repeat
  // ask lands on `DO NOTHING` (zero rows) rather than an exception.
  const written = await query(
    `INSERT INTO subscription_requests (email, plan_requested, note)
     VALUES ($1::text, $2::text, NULLIF($3::text, ''))
     ON CONFLICT (lower(email)) WHERE status = 'PENDING' DO NOTHING
     RETURNING id`,
    [email, normalisePlan(input.plan), input.note.trim()],
  );
  if (written.length === 0) {
    return { ok: true, note: "That email is already in the queue — the answer is coming." };
  }
  return { ok: true, note: `Request received for ${email} (${normalisePlan(input.plan)}). The Super Admin reviews the queue on the Accounts page.` };
}

/**
 * Approve = create the account in the same action (this is why 0013 put no FK
 * on the table), and mark the request. Dismiss = mark only. Either way the
 * request row keeps who decided and when, and the audit row says the same.
 */
export async function handleSubscriptionRequest(
  actor: SessionUser,
  requestId: string,
  decision: "APPROVE" | "DISMISS",
  passwordForNewAccount: string,
  ip: string | null,
): Promise<AdminResult> {
  const rows = await query<Raw>(
    "SELECT id::text AS id, email, plan_requested, status FROM subscription_requests WHERE id = $1::bigint",
    [requestId],
  );
  const request = rows[0];
  if (!request) return { ok: false, status: 404, error: "No such request." };
  if (String(request.status) !== "PENDING") {
    return { ok: false, status: 409, error: `Already ${String(request.status).toLowerCase()} on ${String(request.handled_at ?? "earlier")}.` };
  }
  const email = String(request.email);
  if (decision === "APPROVE") {
    const created = await adminCreateUser(
      actor,
      { email, password: passwordForNewAccount, role: "USER", plan: normalisePlan(request.plan_requested) },
      ip,
    );
    if (!created.ok) return created;
  }
  await query(
    `UPDATE subscription_requests
     SET status = $2::text, handled_at = now(), handled_by_email = $3::text
     WHERE id = $1::bigint`,
    [requestId, decision === "APPROVE" ? "APPROVED" : "DISMISSED", actor.email],
  );
  await query(
    `INSERT INTO audit_logs (actor, actor_role, action, entity_type, entity_id, new_value, request_ip)
     VALUES ($1::text, $2::text, $3::text, 'subscription_requests', $4::text, $5::jsonb, $6::text)`,
    [actor.email, actor.role, `subscription_${decision.toLowerCase()}`, email, { decision }, ip],
  ).catch(() => {});
  return {
    ok: true,
    note:
      decision === "APPROVE"
        ? `Approved: ${email} has a ${normalisePlan(request.plan_requested)} account. Give them the password you set.`
        : `Dismissed the request from ${email}. They can ask again; nothing was created.`,
  };
}
