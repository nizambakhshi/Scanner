/**
 * Password hashing with Node's own scrypt — no native module, no third-party
 * auth service, nothing that can phone home.
 *
 * Format: `scrypt$<salt-hex>$<hash-hex>`. The same format `npm run hash-password`
 * produces for `AUTH_ADMIN_PASSWORD_HASH`, and the same one the `app_users`
 * migration stores, so a hash generated on a laptop and pasted into Vercel verify
 * identically. Never the password itself, and never a comparison that leaks
 * timing: `timingSafeEqual` on the derived keys.
 */

import { randomBytes, scrypt as _scrypt, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";

const scrypt = promisify(_scrypt) as (password: string, salt: Buffer, keylen: number) => Promise<Buffer>;

const KEY_LENGTH = 64;
const ALGO = "scrypt";

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const derived = await scrypt(normalise(password), salt, KEY_LENGTH);
  return `${ALGO}$${salt.toString("hex")}$${derived.toString("hex")}`;
}

export async function verifyPassword(password: string, stored: string | null | undefined): Promise<boolean> {
  if (!stored) return false;
  const [algo, saltHex, hashHex] = stored.split("$");
  if (algo !== ALGO || !saltHex || !hashHex) return false;
  const expected = Buffer.from(hashHex, "hex");
  if (expected.length === 0) return false;
  const derived = await scrypt(normalise(password), Buffer.from(saltHex, "hex"), expected.length);
  return derived.length === expected.length && timingSafeEqual(derived, expected);
}

/** NFKC-normalise and drop a trailing space, so a password manager's accidental
 *  space is not silently a different password. */
function normalise(value: string): string {
  return value.normalize("NFKC").trimEnd();
}

/** Rejects the obviously weak before it reaches the database; returns a message
 *  for the form, or null when the password is acceptable. */
export function passwordProblem(password: string): string | null {
  if (password.length < 10) return "Use at least 10 characters.";
  if (password.length > 200) return "That password is too long to be stored safely.";
  if (/^(.)\1+$/.test(password)) return "That looks too repetitive to be a password.";
  const weak = ["password12", "scanner123", "1234567890", "qwertyuiop", "angelone12"];
  if (weak.some((candidate) => password.toLowerCase().startsWith(candidate))) return "That is a commonly used password.";
  return null;
}

/**
 * The rules for changing a password, with no database and no I/O.
 *
 * "Same as before" is checked on the normalised forms rather than the raw strings,
 * because `normalise()` is what the hash is derived from: a trailing space that
 * disappears at hashing time would otherwise let someone "change" their password to
 * the password they already have, and believe the form when it says otherwise.
 */
export function changePasswordProblem(currentPassword: string, newPassword: string): string | null {
  if (currentPassword.length === 0) return "Enter your current password.";
  if (newPassword.length === 0) return "Enter a new password.";
  if (normalise(currentPassword) === normalise(newPassword)) return "That is the same as your current password.";
  return passwordProblem(newPassword);
}
