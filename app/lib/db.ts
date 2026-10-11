/**
 * PostgreSQL access for the dashboard. Server-side only — nothing here is
 * reachable from the browser, and the connection string never leaves this
 * process.
 *
 * Two details specific to Supabase, because both decide whether a deployment
 * works at all:
 *
 * * **Prepared statements.** Supabase's *transaction* pooler (`…pooler.supabase.com:6543`)
 *   multiplexes clients over server connections, which breaks the named
 *   prepared statements a session pooler assumes. The URL is the only place that
 *   knows which one is configured, so the flag is derived from it rather than
 *   from another variable someone has to remember to keep in sync.
 * * **TLS.** `require` is the default. `disable` exists for a local plaintext
 *   test server and is reported on the health page; `no-verify` would be a
 *   silently broken production setting, so it is accepted (self-hosted proxies
 *   legitimately need it) but always labelled.
 * * **Frozen instances.** On serverless (Vercel) a function instance is frozen
 *   between requests and thawed later, but the TCP sockets it was holding are
 *   killed by the pooler's idle timeout meanwhile. The thawed instance then
 *   writes the next query into a half-dead socket and the driver waits for a
 *   reply that will never come. Every read therefore carries a deadline; hitting
 *   it retires the whole pool, and read-only statements retry once on fresh
 *   sockets — a slow page is survivable, a page that never answers is not.
 */

import postgres, { type Sql } from "postgres";
import { env } from "./env";
import { safeMessage } from "./redact";

/** Row shape the drivers hand back; the mappers in `rows.ts` own the coercion. */
export type Raw = Record<string, unknown>;

type Pool = Sql & { [Symbol.asyncDispose]?: () => Promise<void> };

let client: Pool | null = null;
let lastError: string | null = null;

/** Ceiling for any single database read. Healthy reads against the far-away
 *  pooler are tens of milliseconds; only a stalled or half-dead socket
 *  approaches this. */
const READ_DEADLINE_MS = 3500;
const TIMEOUT_MARK = "database read timed out";

/** Drop the pool so the next `getSql()` opens brand-new sockets. */
function retirePool(): void {
  const existing = client;
  client = null;
  void existing?.end({ timeout: 1 })?.catch(() => {});
}

/**
 * Race a database read against a deadline. Exported (with `isReadTimeout`) for
 * tests — production code always goes through `query()`.
 */
export function withReadDeadline<T>(pending: Promise<T>, ms: number = READ_DEADLINE_MS): Promise<T> {
  // The race can lose to the timer; mark the query promise as handled so its
  // late rejection can never crash the process as an unhandled rejection.
  pending.catch(() => {});
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${TIMEOUT_MARK} after ${ms}ms`)), ms);
    timer.unref?.();
  });
  return Promise.race([pending, deadline]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/** True when `error` is the deadline rejection above (never a driver message). */
export function isReadTimeout(error: unknown): boolean {
  return error instanceof Error && error.message.startsWith(TIMEOUT_MARK);
}

function isTransactionPooler(url: string): boolean {
  return url.includes("pooler.supabase.com") && url.includes(":6543");
}

export function getSql(): Pool {
  if (client) return client;
  if (env.databaseUrl === "") throw new Error("DATABASE_URL is not configured");
  const ssl = env.databaseSsl === "disable" ? false : env.databaseSsl === "no-verify" ? { rejectUnauthorized: false } : "require";
  client = postgres(env.databaseUrl, {
    // A dashboard is a burst reader: one page renders five queries. Keep the
    // pool small so a busy refresh cannot exhaust Supabase's connection slots
    // that the worker needs to keep ingesting.
    max: 4,
    idle_timeout: 20,
    connect_timeout: 10,
    // Recycle connections often: on serverless a socket minutes old has almost
    // certainly been frozen and NAT-expired at least once.
    max_lifetime: 60 * 5,
    // Probe live sockets every 10s so a pooler-side disconnect surfaces as an
    // error the panels can show, instead of a query waiting on a dead write.
    keep_alive: 10,
    ssl,
    prepare: !isTransactionPooler(env.databaseUrl),
    onnotice: () => {},
  }) as Pool;
  return client;
}

/** Last connection failure, with the message stripped of anything DSN-like. */
export function lastConnectionError(): string | null {
  return lastError;
}

export async function healthcheck(): Promise<{ ok: boolean; detail: string }> {
  if (env.databaseUrl === "") return { ok: false, detail: "DATABASE_URL is not set" };
  try {
    const rows = await query<{ version: string; now: string }>(
      "SELECT current_setting('server_version') AS version, now()::text AS now",
    );
    return { ok: true, detail: `PostgreSQL ${rows[0]?.version ?? "?"}` };
  } catch (error) {
    // Postgres drivers echo the DSN they were handed inside their own connection
    // errors; this string is rendered on the health page, so it is redacted and
    // shortened before it can ever reach a browser (§19).
    lastError = safeMessage(error, 160);
    return { ok: false, detail: lastError };
  }
}

export async function closeDb(): Promise<void> {
  const existing = client;
  client = null;
  await existing?.end({ timeout: 2 });
}

/** `SELECT …` with positional parameters, always via `unsafe` so the transaction
 *  pooler never sees a prepared statement it cannot reuse. Every read carries the
 *  deadline above; hitting it retires the pool, and a read-only statement is then
 *  retried once on brand-new sockets — a stalled response on a half-dead
 *  connection is a connection problem, not a query problem. Writes are never
 *  retried: their first attempt may have reached the server before the stall. */
export async function query<T extends Record<string, unknown>>(text: string, params: unknown[] = []): Promise<T[]> {
  const readOnly = /^\s*select\b/i.test(text);
  try {
    return await withReadDeadline(getSql().unsafe<T[]>(text, params as never[]));
  } catch (error) {
    if (!isReadTimeout(error)) throw error;
    retirePool();
    if (!readOnly) throw error;
    return await withReadDeadline(getSql().unsafe<T[]>(text, params as never[]));
  }
}
