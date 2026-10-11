import assert from "node:assert/strict";
import { test } from "node:test";
import { buildEnv, missingRequired, usesPostgres } from "../lib/env";
import { panelError } from "../lib/source";

/**
 * Which environment means which mode, and what a failure is allowed to say.
 *
 * The allowlist test is the one that matters for the deployment: pasting the
 * worker's environment into this app must change nothing, because the dashboard has
 * no field that a broker key can land in.
 */

test("the sample is used only when no database is configured", () => {
  assert.equal(usesPostgres(buildEnv({})), false);
  assert.equal(usesPostgres(buildEnv({ DATABASE_URL: "postgres://user@host:5432/db" })), true);

  // The override exists for tests and airgapped demos, and it beats the URL: an
  // operator who sets DATA_SOURCE=replay must never be shown real numbers.
  assert.equal(usesPostgres(buildEnv({ DATABASE_URL: "postgres://user@host:5432/db", DATA_SOURCE: "replay" })), false);
  assert.equal(usesPostgres(buildEnv({ DATA_SOURCE: "postgres" })), true);
});

test("a missing session secret is reported by name, never by value", () => {
  const missing = missingRequired(buildEnv({ AUTH_SESSION_SECRET: "x".repeat(31) }));
  assert.ok(missing.some((name) => name.startsWith("AUTH_SESSION_SECRET")));
  assert.equal(missing.some((name) => name.includes("x".repeat(31))), false);

  // Replay mode needs no connection string, so it must not be listed as missing.
  assert.deepEqual(missingRequired(buildEnv({ AUTH_SESSION_SECRET: "y".repeat(32) })), []);
  assert.ok(
    missingRequired(buildEnv({ AUTH_SESSION_SECRET: "y".repeat(32), DATA_SOURCE: "postgres" })).some((name) =>
      name.startsWith("DATABASE_URL"),
    ),
  );
});

test("blank-looking values are blank, not secrets of length 4", () => {
  const config = buildEnv({ DATABASE_URL: "   ", AUTH_ADMIN_EMAIL: "  Operator@Example.COM " });
  assert.equal(config.databaseUrl, "");
  assert.equal(config.adminEmail, "operator@example.com");
  assert.equal(usesPostgres(config), false);
});

test("session hours are clamped to a day at most", () => {
  assert.equal(buildEnv({ AUTH_SESSION_HOURS: "9999" }).sessionHours, 168);
  assert.equal(buildEnv({ AUTH_SESSION_HOURS: "0" }).sessionHours, 1);
  assert.equal(buildEnv({ AUTH_SESSION_HOURS: "not-a-number" }).sessionHours, 12);
});

test("no broker or messaging credential is ever read by this app", () => {
  const config = buildEnv({
    ANGEL_API_KEY: "visible",
    ANGEL_CLIENT_CODE: "visible",
    ANGEL_PASSWORD: "visible",
    TELEGRAM_BOT_TOKEN: "visible",
    DATABASE_URL: "postgres://u@h/d",
  });
  assert.deepEqual(
    Object.keys(config).sort(),
    [
      "adminEmail",
      "adminPasswordHash",
      "appUrl",
      "databaseSsl",
      "databaseUrl",
      "dataSource",
      "nodeEnv",
      "sessionHours",
      "sessionSecret",
    ].sort(),
  );
  assert.equal(
    Object.keys(config).some((key) => /angel|telegram|totp|pin|api/i.test(key)),
    false,
  );
});

test("an error shown to an operator is short and carries no connection string", () => {
  const dsn = "postgres://operator:sup3rs3cr3t@db.internal.example:5432/postgres";
  const message = panelError(new Error(`connection failed while dialing ${dsn}\nsecond line nobody needs`));
  assert.ok(message.length <= 200);
  assert.ok(!message.includes("sup3rs3cr3t"), "a password must never reach a panel");
  assert.ok(!message.includes("second line"));
});
