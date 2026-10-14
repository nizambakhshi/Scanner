/**
 * Sessions.
 *
 * A signed, stateless cookie (`HMAC-SHA256` over the payload with
 * `AUTH_SESSION_SECRET`) that is additionally checked against `app_sessions` when
 * a database is configured. That combination is deliberate:
 *
 * * **Stateless signature** keeps the login path working in the replay sample and
 *   on a cold serverless invoke, where a database round trip is the only way to
 *   know who is calling — and it cannot be forged without the secret.
 * * **`app_sessions` rows**, when a database exists, give the operator the thing a
 *   JWT cannot: logout from every device at once (`revoked_at`), an expiry the
 *   server owns, and a record of the IP and user agent that appeared on §16's
 *   audit list.
 *
 * When `app_sessions` is in use, a valid signature whose row is missing or revoked
 * is a 401 — fail closed, never fall back to trusting the cookie alone.
 */

import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { env, usesPostgres } from "../env";
import { query, type Raw } from "../db";
import type { SessionUser } from "./users";

export const SESSION_COOKIE = "scanner_session";
const MAX_AGE_SECONDS = env.sessionHours * 3600;

export type SessionPayload = {
  /** `app_users.id`, or the admin email in the replay sample. */
  sub: string;
  email: string;
  role: string;
  /** Epoch seconds. */
  exp: number;
  /** Session id; the `app_sessions` primary key when a database is configured. */
  jti: string;
};

function secret(): Buffer {
  if (env.sessionSecret.length < 32) {
    throw new Error("AUTH_SESSION_SECRET is missing or shorter than 32 characters");
  }
  return Buffer.from(env.sessionSecret, "utf8");
}

const encode = (value: string) => Buffer.from(value, "utf8").toString("base64url");
const decode = (value: string) => Buffer.from(value, "base64url").toString("utf8");

function sign(data: string): string {
  return createHmac("sha256", secret()).update(data).digest("base64url");
}

function verifySignature(data: string, signature: string): boolean {
  const expected = Buffer.from(sign(data), "utf8");
  const provided = Buffer.from(signature, "utf8");
  return expected.length === provided.length && timingSafeEqual(expected, provided);
}

export function issueToken(payload: SessionPayload): string {
  const data = encode(JSON.stringify(payload));
  return `${data}.${sign(data)}`;
}

export function readToken(token: string | undefined): SessionPayload | null {
  if (!token) return null;
  const [data, signature] = token.split(".");
  if (!data || !signature) return null;
  if (!verifySignature(data, signature)) return null;
  try {
    const payload = JSON.parse(decode(data)) as SessionPayload;
    if (typeof payload.exp !== "number" || payload.exp * 1000 <= Date.now()) return null;
    if (typeof payload.email !== "string" || typeof payload.jti !== "string") return null;
    return payload;
  } catch {
    return null;
  }
}

/**
 * Store a session and set the cookie.
 *
 * The `app_sessions` insert happens first when a database is configured: a user
 * who is told they are signed in but whose session cannot be found on the next
 * request is worse than a login that visibly fails.
 */
export async function createSession(user: SessionUser, request: { ip: string | null; userAgent: string | null }) {
  const payload: SessionPayload = {
    sub: user.id,
    email: user.email,
    role: user.role,
    exp: Math.floor((Date.now() + MAX_AGE_SECONDS * 1000) / 1000),
    jti: randomUUID(),
  };
  if (usesPostgres()) {
    await query(
      `INSERT INTO app_sessions (id, user_id, expires_at, request_ip, user_agent)
       VALUES ($1::text, $2::bigint, to_timestamp($3::bigint), $4::text, $5::text)`,
      [payload.jti, payload.sub, payload.exp, request.ip, request.userAgent],
    );
    await pruneDeadSessions();
  }
  const store = await cookies();
  store.set(SESSION_COOKIE, issueToken(payload), {
    httpOnly: true,
    secure: env.nodeEnv === "production",
    sameSite: "lax",
    path: "/",
    maxAge: MAX_AGE_SECONDS,
  });
  return payload;
}

/**
 * Drop rows that cannot authenticate anything.
 *
 * `readToken` rejects a cookie whose `exp` has passed before the database is
 * consulted, and `expires_at` is that same instant — so an expired row is
 * already unreachable, revoked or not. It was never deleted, though: the live
 * table was 22 rows, 22 of them expired, and it had never been autovacuumed.
 * Login is where this runs: the database is already open, the write is one
 * indexed DELETE on a table this path keeps small, and nobody is waiting on a
 * board to refresh. Failure is deliberately silent — cleanup must never turn a
 * successful sign-in into an error page.
 */
async function pruneDeadSessions(): Promise<void> {
  try {
    await query("DELETE FROM app_sessions WHERE expires_at < now()");
  } catch {
    // The next sign-in tries again; a full table is not worth a broken login.
  }
}

export async function destroySession(userId?: string) {
  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;
  const payload = readToken(token);
  if (payload && usesPostgres()) {
    await query("UPDATE app_sessions SET revoked_at = now() WHERE id = $1::text", [payload.jti]);
  }
  if (userId && usesPostgres()) {
    // "log out everywhere" is the reason the session lives in a table at all.
    await query("UPDATE app_sessions SET revoked_at = now() WHERE user_id = $1::bigint AND revoked_at IS NULL", [userId]);
  }
  store.delete(SESSION_COOKIE);
}

export async function getCurrentUser(): Promise<SessionUser | null> {
  const payload = readToken((await cookies()).get(SESSION_COOKIE)?.value);
  if (!payload) return null;
  if (!usesPostgres()) {
    // Replay mode has no user table: the single operator from the environment is
    // the only subject a valid cookie can ever describe.
    return { id: payload.sub, email: payload.email, role: payload.role, displayName: null, source: "replay" };
  }
  let rows: Raw[];
  try {
    rows = await query<Raw>(
      `SELECT s.id AS jti, s.user_id::text AS sub, s.expires_at, s.revoked_at,
              u.email, u.role, u.display_name, u.is_active
       FROM app_sessions s
       JOIN app_users u ON u.id = s.user_id
       WHERE s.id = $1::text`,
      [payload.jti],
    );
  } catch {
    // A stalled or failed session read sends the operator back to sign-in rather
    // than to a raw 500: an unverifiable session is exactly what "not signed in"
    // means for authorization (§16 fail-closed), and the deadline in `lib/db.ts`
    // guarantees this branch arrives in seconds, not after minutes of hang.
    return null;
  }
  const row = rows[0];
  if (!row || row.revoked_at || row.sub !== payload.sub || row.email !== payload.email) return null;
  if (row.is_active === false) return null;
  // last_seen_at is written at most once a minute so a 30-second refresh loop
  // cannot turn a page view into five database writes. It is telemetry, not the
  // authorization itself, so a failure here must not blank the page.
  await query(
    "UPDATE app_sessions SET last_seen_at = now() WHERE id = $1::text AND last_seen_at < now() - interval '60 seconds'",
    [payload.jti],
  ).catch(() => {});
  return {
    id: String(row.sub),
    email: String(row.email),
    role: String(row.role),
    displayName: row.display_name === null ? null : String(row.display_name),
    source: "postgres",
  };
}

/** For pages: send the visitor to the login form instead of rendering a shell. */
export async function requireUserPage(): Promise<SessionUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}

/** For route handlers: `null` means respond 401, not redirect. */
export async function requireUserApi(): Promise<SessionUser | null> {
  return getCurrentUser();
}
