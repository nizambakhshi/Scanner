/**
 * The Accounts module's rules, without the database.
 *
 * The Super Admin's account page creates users, resets any password, flips
 * plans and actives, and works the subscription queue. Everything those
 * actions need to *check* lives here as pure functions, so the API route and
 * the tests agree on what a bad request is — and so a future change to the
 * plan vocabulary has exactly one place to change.
 *
 * The plans themselves (`lib/plans.ts` is deliberately not a thing): BASIC
 * shows the top three symbols on the daily watchlist, UNLIMITED shows all of
 * them, and a database that has not applied migration 0013 behaves as
 * UNLIMITED for everyone — grandfathering is the migration's job, and "no
 * column yet" must never lock a paying-or-not user out of their board.
 */

export const PLANS = ["BASIC", "UNLIMITED"] as const;
export type Plan = (typeof PLANS)[number];

/** The watchlist size a plan entitles the holder to see. */
export const BASIC_WATCHLIST_LIMIT = 3;

export const ROLES = ["SUPER_ADMIN", "ADMIN", "USER"] as const;

/** What the public Subscribe page accepts: a plain user picks a plan, nobody
 *  asks an admin role into existence through a public form. */
export const REQUESTABLE_PLANS: readonly Plan[] = PLANS;

/** The strict check against the stored vocabulary — upper-case only. Forms that
 *  send mixed case go through `normalisePlan` first, never this. */
export function isPlan(value: unknown): value is Plan {
  return typeof value === "string" && (PLANS as readonly string[]).includes(value);
}

export function normalisePlan(value: unknown): Plan {
  return String(value ?? "").trim().toUpperCase() === "UNLIMITED" ? "UNLIMITED" : "BASIC";
}

/** Same shape test the login form uses — nothing fancier, no normalization services. */
export function emailProblem(email: string): string | null {
  const value = email.trim();
  if (value === "") return "Enter an email address.";
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) return "That does not look like an email address.";
  if (value.length > 254) return "That email is too long to store.";
  return null;
}

/**
 * Validate a create-account request. The role is checked against the full
 * vocabulary on purpose: the page is admin-only and an admin *should* be able
 * to make another admin — but not through a public endpoint, which is why
 * `/api/subscribe` never calls this.
 */
export function createAccountProblem(input: {
  email: string;
  role: string;
  plan: string;
}): string | null {
  const email = emailProblem(input.email);
  if (email) return email;
  if (!(ROLES as readonly string[]).includes(input.role)) return "That role does not exist.";
  if (!isPlan(input.plan)) return "That plan does not exist — BASIC or UNLIMITED.";
  return null;
}

/**
 * Validate a public subscription request. The note is the applicant's own
 * words: length-capped here so the table never holds a wall of text, and read
 * back as plain text everywhere.
 */
export function subscribeProblem(input: { email: string; plan: string; note: string }): string | null {
  const email = emailProblem(input.email);
  if (email) return email;
  if (!isPlan(input.plan)) return "Choose a plan: Basic or Unlimited.";
  if (input.note.length > 500) return "Keep the note under 500 characters.";
  return null;
}

/**
 * The paywall itself, as one pure function over an already-sorted list:
 * a BASIC account sees the top three. Everything about *why* the list is
 * ordered (score, state, symbol) belongs to the sort path — the cap never
 * reorders, it only truncates, so what the operator sees for a row is what
 * the user sees.
 */
export function capForPlan<T>(rows: T[], plan: Plan | "UNKNOWN"): T[] {
  if (plan !== "BASIC") return rows;
  return rows.slice(0, BASIC_WATCHLIST_LIMIT);
}
