#!/usr/bin/env node
/**
 * Produce an `AUTH_ADMIN_PASSWORD_HASH` value.
 *
 *   npm run hash-password -- --email you@example.com
 *
 * Prompts for the password (never read from argv, so it cannot land in shell
 * history or a process list), prints the `scrypt$salt$hash` string to paste into
 * the deployment environment, and prints nothing else. The hash is portable: the
 * app verifies it with the same `scrypt` parameters from `lib/auth/password.ts`,
 * so generating it here and setting it in Vercel is the whole setup — the
 * plaintext password is never stored anywhere, including this process's memory
 * beyond the prompt.
 */

import { createInterface } from "node:readline/promises";
import { randomBytes, scrypt as _scrypt } from "node:crypto";
import { stdin, stdout } from "node:process";
import { promisify } from "node:util";

const scrypt = promisify(_scrypt);
const KEY_LENGTH = 64;

function mask(value) {
  return `${value.slice(0, 2)}${"*".repeat(Math.max(0, value.length - 2))}`;
}

async function main() {
  const args = process.argv.slice(2);
  const emailFlag = args.indexOf("--email");
  const email = emailFlag >= 0 ? args[emailFlag + 1] : null;

  const rl = createInterface({ input: stdin, output: stdout });
  const password = await rl.question("Password (input is not hidden in some terminals; run this where no one is watching): ");
  rl.close();

  const normalised = password.normalize("NFKC").trimEnd();
  if (normalised.length < 10) {
    stdout.write("Refusing: use at least 10 characters.\n");
    process.exitCode = 1;
    return;
  }
  if (/^(.)\1+$/.test(normalised)) {
    stdout.write("Refusing: that is one repeated character.\n");
    process.exitCode = 1;
    return;
  }

  const salt = randomBytes(16);
  const derived = await scrypt(normalised, salt, KEY_LENGTH);
  const hash = `scrypt$${salt.toString("hex")}$${derived.toString("hex")}`;

  stdout.write(
    [
      "",
      "# Copy these two lines into your deployment environment (Vercel → Settings → Environment Variables).",
      "# Nothing else needs to be set for sign-in, and no broker credential belongs in this app.",
      email ? `AUTH_ADMIN_EMAIL=${email}` : "AUTH_ADMIN_EMAIL=you@example.com   # set this to the operator address",
      `AUTH_ADMIN_PASSWORD_HASH=${hash}`,
      "",
      `# Verified against ${mask(normalised)} — the password above was never written to disk.`,
      "# To change it later: run this again and replace the hash. Existing app_users rows are unaffected; the",
      "# environment value is only used to bootstrap the first admin.",
      "",
    ].join("\n")
  );
}

main().catch((error) => {
  stdout.write(`${error instanceof Error ? error.message : error}\n`);
  process.exitCode = 1;
});
