#!/usr/bin/env node
/**
 * Reset an operator's password in `app_users`, in place.
 *
 *   npm run reset-password -- --email you@example.com
 *   npm run reset-password -- --email you@example.com --env-file ../Scanner/app/.env.local
 *
 * This is the recovery path for the case the dashboard cannot serve: the password is
 * lost, or the sign-in throttle is holding the operator out. There is no emailed reset
 * link because this app has no mail transport, and an unauthenticated endpoint that
 * issues tokens is an attack surface rather than a way back — so what proves ownership
 * is access to `DATABASE_URL`, which is a file on this machine.
 *
 * Conventions worth keeping:
 *
 * * The password is prompted, twice, without echoing. It is never read from `argv`
 *   (that would put it in shell history and in `ps`) and never printed.
 * * The hash comes from `lib/auth/password.ts`, the same code the login path verifies
 *   with. A locally re-implemented `scrypt` call is how a reset ends up producing a hash
 *   the app will not accept.
 * * Every session for the account is revoked. Changing a password because someone else
 *   knew it is also the moment to throw out the cookies they may already hold.
 * * The database being written to is identified (masked DSN + `current_database()`) and
 *   must be typed back, because two copies of this app exist on one machine and a reset
 *   aimed at the wrong one fails silently.
 */
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { stdin, stdout } from "node:process";

const argv = process.argv.slice(2);
const flag = (name) => {
  const i = argv.indexOf(`--${name}`);
  return i === -1 ? null : argv[i + 1];
};

const email = (flag("email") ?? "").trim().toLowerCase();
const envFile = resolve(flag("env-file") ?? ".env.local");

if (email === "" || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
  console.error("usage: npm run reset-password -- --email you@example.com [--env-file .env.local]");
  process.exit(2);
}

let loaded = 0;
try {
  for (const line of readFileSync(envFile, "utf8").split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (!m) continue;
    process.env[m[1]] = m[2];
    loaded += 1;
  }
} catch (error) {
  console.error(`cannot read ${envFile}: ${error.message}`);
  process.exit(2);
}

const url = process.env.DATABASE_URL ?? "";
if (url === "") {
  console.error(
    `${envFile} sets no DATABASE_URL — this copy authenticates against AUTH_ADMIN_PASSWORD_HASH, ` +
      "so change that variable instead (`npm run hash-password` prints a new one).",
  );
  process.exit(1);
}

/** Scheme + host + database, credential never included. */
const masked = url.replace(/^(postgres(?:ql)?):\/\/[^@]*@/, "$1://•••@").replace(/:[^:/@]+@/, ":•••@");

// One interface for the whole run, created *before* the first await. A piped answer
// arrives while the database is still being contacted; if nothing is listening at that
// moment the line is emitted to nobody and dropped, and the script then dies with
// "readline was closed" — which is exactly what happens when stdin is a pipe (a test,
// a heredoc) rather than a terminal. Buffering from construction avoids it, and a
// terminal user's keystrokes take the same queue.
const rl = createInterface({ input: stdin, output: stdout, terminal: false });
const queued = rl[Symbol.asyncIterator]();

/**
 * `terminal: false` is what keeps the answer off the screen — readline echoes only what
 * it writes itself, and with no terminal it writes nothing back. The value goes to the
 * caller and is then dropped: never logged, never written to disk.
 */
async function ask(question) {
  stdout.write(question);
  const { value, done } = await queued.next();
  if (done) throw new Error("stdin closed before an answer arrived");
  return value;
}

const { query, healthcheck, closeDb } = await import("../lib/db.ts");
const { hashPassword, passwordProblem } = await import("../lib/auth/password.ts");

async function main() {
  const health = await healthcheck();
  if (!health.ok) {
    const { lastConnectionError } = await import("../lib/db.ts");
    console.error(`database unreachable: ${health.detail}`);
    console.error(`               ${(lastConnectionError() ?? "").replace(/:\/\/[^@\s]*@/, "://•••@").split("\n")[0]}`);
    process.exitCode = 1;
    return;
  }
  const [server] = await query(
    `select current_database() as db, coalesce(inet_server_addr()::text, '(not reported)') as addr`,
  );

  stdout.write(
    [
      "",
      `env file  ${envFile}  (${loaded} variables)`,
      `database  ${masked}`,
      `          answers as ${server.db} @ ${server.addr}`,
      `account   ${email}`,
      "",
      "Nothing typed here is echoed, including the database name below.",
      "Type the database name above to confirm this is the copy you mean. Anything else stops here.",
      "",
    ].join("\n"),
  );

  const confirm = (await ask("confirm > ")).trim();
  if (confirm !== server.db) {
    stdout.write("Not confirmed — nothing changed.\n");
    process.exitCode = 1;
    return;
  }

  const account = (
    await query("SELECT id::text AS id, email FROM app_users WHERE lower(email) = lower($1::text)", [email])
  )[0];
  if (!account) {
    stdout.write(
      `No app_users row for ${email} in ${server.db} — nothing changed.\n` +
        "If this database has no accounts yet, sign in once to bootstrap the admin from AUTH_ADMIN_EMAIL.\n",
    );
    process.exitCode = 1;
    return;
  }

  const first = await ask("New password (input is hidden): ");
  const second = await ask("Retype it: ");
  if (first !== second) {
    stdout.write("The two entries did not match — nothing changed.\n");
    process.exitCode = 1;
    return;
  }

  // The same rules the dashboard's form enforces, from the same module, so a terminal
  // cannot be used to install a password the sign-in page would later reject.
  const problem = passwordProblem(first.normalize("NFKC").trimEnd());
  if (problem) {
    stdout.write(`Refusing: ${problem} Nothing changed.\n`);
    process.exitCode = 1;
    return;
  }

  const stored = await hashPassword(first);
  await query("UPDATE app_users SET password_hash = $2::text WHERE id = $1::bigint", [account.id, stored]);
  const revoked = await query(
    "UPDATE app_sessions SET revoked_at = now() WHERE user_id = $1::bigint AND revoked_at IS NULL RETURNING id",
    [account.id],
  );

  stdout.write(
    `\nPassword replaced for ${account.email} (user ${account.id}) in ${server.db}.\n` +
      `${revoked.length} session${revoked.length === 1 ? "" : "s"} revoked — sign in again everywhere, this page included.\n`,
  );
}

try {
  await main();
} catch (error) {
  // A driver error can embed the connection string, and this output is meant to be
  // pasteable into a chat.
  const text = String(error?.message ?? error).replace(/:\/\/[^@\s]*@/, "://•••@");
  console.error(`reset failed: ${text.split("\n")[0]}`);
  process.exitCode = 1;
} finally {
  rl.close();
  await closeDb().catch(() => {});
}
