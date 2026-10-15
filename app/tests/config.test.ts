import assert from "node:assert/strict";
import { test } from "node:test";
import { buildEnv, missingRequired, usesPostgres } from "../lib/env";
import { panelError } from "../lib/source";

/**
 * Which environment means which mode, and what a failure is allowed to say.
 *
 * The credential test below is the one that matters for the deployment. It used to
 * read "no broker or messaging credential is ever read by this app", which was true
 * while the dashboard could not send a Telegram message at all. On 2026-10-14 the
 * operator asked for alert-strategy configuration with a Telegram destination and a
 * Send-test button, so the second half of that claim was deliberately given up:
 * `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are now read, as the *fallback* for the
 * row in `ops_settings['telegram']`.
 *
 * What has not been given up, and what the assertions below still enforce:
 * no broker credential of any kind, ever; the token never leaves this process as a
 * value (see `tests/telegram.test.ts` for the view, log and audit paths); and the
 * page cannot read what it is showing.
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

test("a broker credential is never read; Telegram is read only as a fallback", () => {
  const config = buildEnv({
    ANGEL_API_KEY: "visible",
    ANGEL_CLIENT_CODE: "visible",
    ANGEL_PASSWORD: "visible",
    ANGEL_TOTP_SECRET: "visible",
    TELEGRAM_BOT_TOKEN: "123456:AAAsecret-token-value",
    TELEGRAM_CHAT_ID: "-1009876543210",
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
      "telegramBotToken",
      "telegramChatId",
    ].sort(),
  );
  // The half of the old claim that stays: nothing that could log in to an exchange,
  // name a client, or sign a TOTP. `telegram*` is the one exception the operator
  // asked for on 2026-10-14, and `tests/telegram.test.ts` is what keeps it contained.
  assert.equal(
    Object.keys(config).some((key) => /angel|totp|pin|api|trade|order/i.test(key)),
    false,
  );
  // Read, but only as a *fallback* — precedence and the masked view are pinned in
  // tests/telegram.test.ts, not here.
  assert.equal(config.telegramBotToken, "123456:AAAsecret-token-value");
  assert.equal(config.telegramChatId, "-1009876543210");
});

test("an error shown to an operator is short and carries no connection string", () => {
  const dsn = "postgres://operator:sup3rs3cr3t@db.internal.example:5432/postgres";
  const message = panelError(new Error(`connection failed while dialing ${dsn}\nsecond line nobody needs`));
  assert.ok(message.length <= 200);
  assert.ok(!message.includes("sup3rs3cr3t"), "a password must never reach a panel");
  assert.ok(!message.includes("second line"));
});
