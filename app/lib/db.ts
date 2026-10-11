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
 */

import postgres, { type Sql } from "postgres";
import { env } from "./env";
import { safeMessage } from "./redact";

/** Row shape the drivers hand back; the mappers in `rows.ts` own the coercion. */
export type Raw = Record<string, unknown>;

type Pool = Sql & { [Symbol.asyncDispose]?: () => Promise<void> };

let client: Pool | null = null;
let lastError: string | null = null;

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
    max_lifetime: 60 * 30,
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
    const rows = await getSql().unsafe<{ version: string; now: string }[]>(
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
 *  pooler never sees a prepared statement it cannot reuse. */
export async function query<T extends Record<string, unknown>>(text: string, params: unknown[] = []): Promise<T[]> {
  return getSql().unsafe<T[]>(text, params as never[]);
}
