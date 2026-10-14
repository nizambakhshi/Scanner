import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/auth/session";
import { authSetupProblem } from "@/lib/auth/users";
import { env } from "@/lib/env";
import { source } from "@/lib/source";
import { LoginForm } from "@/components/LoginForm";
import { SimulatedBanner } from "@/components/SimulatedBanner";
import { ThemeToggle } from "@/components/ThemeToggle";

export const dynamic = "force-dynamic";

/**
 * Sign-in.
 *
 * This is the only page reachable without a session, so it deliberately reads
 * nothing but the source label: no watchlist, no signals, no counts. An
 * unauthenticated response must not be able to carry a single number about what is
 * on the watchlist today.
 */
export default async function LoginPage() {
  const user = await getCurrentUser();
  if (user) redirect("/");
  const meta = await source().meta();
  const problem = authSetupProblem();

  return (
    <div className="min-h-screen">
      {/* The palette is chosen before sign-in as well: this is the page an operator
          sees first, and a board they cannot read is a bad way to start a session.
          In flow above the banner rather than pinned over it — the banner wraps on a
          narrow screen, and a control that lands on top of "SIMULATED DATA" is worse
          than a control that sits one row higher. */}
      <div className="flex justify-end px-4 pt-3">
        <ThemeToggle />
      </div>
      <SimulatedBanner meta={meta} />
      <div className="mx-auto flex min-h-[calc(100vh-4rem)] max-w-[1400px] items-center justify-center px-4 py-10">
        <div className="w-full max-w-[380px]">
          <div className="mb-5">
            <p className="eyebrow">NSE · 1m · intraday</p>
            <h1 className="font-display mt-1 text-[24px] font-semibold uppercase tracking-[0.05em]">Bullish Scanner</h1>
            <p className="mt-1.5 text-[13px] leading-snug text-muted">
              The operator view of the daily watchlist, the alert feed and the delivery queue. Read-only: this
              dashboard cannot place, modify or cancel an order.
            </p>
          </div>

          <div className="card px-5 py-5">
            <LoginForm setupProblem={problem} />
          </div>

          <p className="mt-4 text-[11px] leading-snug text-muted">
            Eight failed attempts from one address in fifteen minutes locks it out. A session lasts {env.sessionHours}{" "}
            hours from sign-in — it does not extend with use, and signing out revokes it in the database as well as
            clearing this browser.
          </p>
          {/* Deliberate, and worth stating where someone who is locked out will read it:
              there is no emailed link, because this app sends no mail and an endpoint that
              hands out reset tokens to whoever names an address is a door, not a recovery
              path. Both real ways back in are on this machine or behind this form. */}
          <p className="mt-2 text-[11px] leading-snug text-muted">
            Forgotten password? Change it from <span className="num">Account</span> while signed in, or run{" "}
            <span className="num">npm run reset-password</span> where the database connection string lives.
          </p>
        </div>
      </div>
    </div>
  );
}
