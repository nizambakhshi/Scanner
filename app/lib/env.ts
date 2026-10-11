/**
 * Environment reading for the dashboard — the only place `process.env` is touched.
 *
 * Two rules the rest of the file exists to enforce:
 *
 * 1. This app is a *reader*. It needs a PostgreSQL connection and a session
 *    secret, and nothing else. Broker credentials (`ANGEL_*`), the Telegram bot
 *    token and the TOTP seed belong to the worker process, which runs elsewhere.
 *    A dashboard that can place no trades and send no messages should not be able
 *    to read the keys that could; if this list ever grows with an `ANGEL_` name,
 *    that is a design mistake, not a missing variable.
 * 2. No value is ever logged. `missingRequired()` returns *names*, which are safe
 *    to show an operator; the values never leave this module.
 */

const raw = process.env;

function str(value: string | undefined, fallback = ""): string {
  return (value ?? "").trim() === "" ? fallback : (value as string).trim();
}

function int(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(str(value, ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * The resolved configuration, built from a raw record so a test can ask "what would
 * this app do with these variables" without restarting the process.
 */
export function buildEnv(source: Record<string, string | undefined>) {
  return {
    /** Direct PostgreSQL (Supabase) connection. Absent → the app serves the replay sample. */
    databaseUrl: str(source.DATABASE_URL),
    /**
     * TLS mode for the database connection. `require` is the default and is what
     * Supabase expects; `disable` exists only for a local plaintext test server and
     * is reported on the health page so it can never be a quiet production choice.
     */
    databaseSsl: str(source.DATABASE_SSL, "require") as "require" | "disable" | "no-verify",
    /** Overrides the automatic choice for tests / airgapped demos. */
    dataSource: str(source.DATA_SOURCE, "auto") as "auto" | "postgres" | "replay",

    adminEmail: str(source.AUTH_ADMIN_EMAIL).toLowerCase(),
    /** `scrypt$<salt>$<hash>` from `npm run hash-password`. Never the password itself. */
    adminPasswordHash: str(source.AUTH_ADMIN_PASSWORD_HASH),
    sessionSecret: str(source.AUTH_SESSION_SECRET),
    sessionHours: Math.min(Math.max(int(source.AUTH_SESSION_HOURS, 12), 1), 168),
    appUrl: str(source.APP_URL, "http://localhost:3000"),
    nodeEnv: str(source.NODE_ENV, "development"),
  } as const;
}

export type AppEnv = ReturnType<typeof buildEnv>;

export const env = buildEnv(raw);

export const IS_PRODUCTION = env.nodeEnv === "production";

/** Rows the replay fixture ships with, so the dashboard is reviewable without a database. */
export const FIXTURE_PATH = "data/replay-fixture.json";

/**
 * Names of the variables needed for the mode this process is in — never their
 * values. `postgres` needs a connection string; the session layer always needs a
 * secret; the admin bootstrap needs an email and a hash.
 */
export function missingRequired(config: AppEnv = env): string[] {
  const missing: string[] = [];
  if (config.sessionSecret.length < 32) missing.push("AUTH_SESSION_SECRET (32+ characters)");
  if (usesPostgres(config) && config.databaseUrl === "") missing.push("DATABASE_URL");
  return missing;
}

/** True when the app should read the real database instead of the replay sample. */
export function usesPostgres(config: AppEnv = env): boolean {
  if (config.dataSource === "replay") return false;
  if (config.dataSource === "postgres") return true;
  return config.databaseUrl !== "";
}
