/**
 * Source selection. One function decides where every number on the dashboard
 * comes from, and the decision is reported on the health page.
 *
 * The rule that matters: when a database is configured, this app **never** falls
 * back to the replay sample. A query that fails must surface as a failed panel.
 * Quietly substituting synthetic candles for real ones would be the single most
 * dangerous thing a tool like this can do — the sample exists so the dashboard can
 * be built and reviewed without a database, not so it can keep smiling when the
 * database is down.
 */

import { env, missingRequired, usesPostgres } from "./env";
import { safeMessage } from "./redact";
import { healthcheck } from "./db";
import { postgresSource } from "./source-postgres";
import { fixtureError, replaySource } from "./source-replay";
import type { ScannerSource } from "./types";

export function source(): ScannerSource {
  return usesPostgres() ? postgresSource : replaySource;
}

export type SourceStatus = {
  name: ScannerSource["name"];
  /** Why this source: configured, or which variable is missing. Names only. */
  reason: string;
  /** Variable *names* still missing for the chosen mode. */
  missing: string[];
  database: { ok: boolean; detail: string } | null;
  sample: { readable: boolean; detail: string | null };
  /** TLS mode in effect — `disable` is worth stating out loud. */
  databaseSsl: string;
  /** True when the transaction pooler was detected from the URL shape. */
  preparedStatements: boolean;
};

export async function describeSource(): Promise<SourceStatus> {
  const usingPostgres = usesPostgres();
  const database = usingPostgres ? await healthcheck() : null;
  return {
    name: usingPostgres ? "postgres" : "replay",
    reason: usingPostgres
      ? env.dataSource === "postgres"
        ? "DATA_SOURCE=postgres"
        : "DATABASE_URL is configured"
      : env.dataSource === "replay"
        ? "DATA_SOURCE=replay (database ignored)"
        : "DATABASE_URL is not configured — serving the offline replay sample",
    missing: missingRequired(),
    database,
    sample: { readable: fixtureError() === null, detail: fixtureError() },
    databaseSsl: usingPostgres ? env.databaseSsl : "n/a",
    preparedStatements: usingPostgres && !env.databaseUrl.includes(":6543"),
  };
}

/**
 * A panel's error text: factual, one line, redacted. A driver is free to embed the
 * connection string it was handed in its own failure message, and a panel prints
 * whatever it is given, so redaction happens here rather than at every call site.
 */
export function panelError(error: unknown): string {
  return safeMessage(error);
}
