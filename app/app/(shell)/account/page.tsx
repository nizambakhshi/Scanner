import { requireUserPage } from "@/lib/auth/session";
import { accountFacts } from "@/lib/auth/users";
import { PasswordForm } from "@/components/PasswordForm";
import { Panel, PageHeading, ErrorNote } from "@/components/ui";
import { ago, dateTime, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { renderNow } from "@/lib/render-time";

export const dynamic = "force-dynamic";

/**
 * The operator's own account: what the dashboard knows about this login, and the
 * one thing worth changing here — the password.
 *
 * There is deliberately no "forgot password" link on the sign-in page. This app
 * has no email transport, so a reset token could only be delivered by a route that
 * a stranger can call, and a stranger-called route that emits a usable token is a
 * way in rather than a way back. The supported path for a lost password is the
 * operator's own terminal: `npm run reset-password`, which needs the database
 * connection string and therefore the machine that holds it.
 */
export default async function AccountPage() {
  const user = await requireUserPage();
  const facts = await panel(() => accountFacts(user));
  const read = ok(facts) ? facts.data : null;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Account"
        subtitle="Who this dashboard thinks you are, and the password that gets you in. Changing it signs out every other session — a session you no longer recognise is the reason that is the default."
      />

      {read === null ? (
        <ErrorNote title="Account details unread" detail={ok(facts) ? "no data" : facts.error} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-3">
          <Panel title="Signed in as">
            <dl className="divide-y divide-line text-[13px]">
              <Row label="Email" value={read.email} />
              <Row label="Role" value={read.role} />
              <Row label="Display name" value={orDash(read.displayName)} />
              <Row label="User table" value={read.canChangePassword ? "app_users (Postgres)" : "environment (replay)"} />
              <Row
                label="Last sign-in"
                value={
                  read.lastLoginAt === null
                    ? "not recorded"
                    : `${dateTime(read.lastLoginAt)} · ${ago(read.lastLoginAt, renderNow())}`
                }
              />
              <Row
                label="Live sessions"
                value={read.liveSessions === null ? "not tracked" : String(read.liveSessions)}
              />
            </dl>
          </Panel>

          <div className="lg:col-span-2">
            <Panel
              title="Change password"
              hint={read.canChangePassword ? "requires the current one" : "no database here"}
            >
              <PasswordForm canChange={read.canChangePassword} />
            </Panel>
          </div>
        </div>
      )}

      <Panel title="If the password is lost" hint="and why there is no email link">
        <div className="space-y-2 px-4 py-3 text-[12px] leading-snug text-ink-2">
          <p>
            Run <span className="num">npm run reset-password</span> on the machine that holds this dashboard&rsquo;s
            database connection. It prompts for the new password twice, never takes it as an argument, checks it against
            the same rules as this form, and overwrites <span className="num">app_users.password_hash</span> for the
            address you name. Every session is revoked, so this page&rsquo;s own sign-in is the thing that gets signed
            out.
          </p>
          <p className="text-muted">
            No emailed reset link exists because no mail transport is configured, and an unauthenticated endpoint that
            hands out tokens is an attack surface rather than a recovery path. The dashboard is also gated by a country
            rule at the tunnel, which is a second wall in front of this form — not a substitute for the current-password
            check above.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-2.5">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="num truncate text-[13px] text-ink" title={value}>
        {value}
      </dd>
    </div>
  );
}
