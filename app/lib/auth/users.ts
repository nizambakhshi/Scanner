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
import { verifyPassword } from "./password";

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
