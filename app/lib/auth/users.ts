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
import { hashPassword, verifyPassword } from "./password";

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
    await dbRecordAttempt(normalised, request.ip, true);
    await query("UPDATE app_users SET last_login_at = now() WHERE id = $1::bigint", [user.id]);
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
