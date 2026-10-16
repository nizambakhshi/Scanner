import { Panel, ErrorNote, PageHeading } from "@/components/ui";
import { AccountsBoard } from "@/components/AccountsBoard";
import { requireAdminPage } from "@/lib/auth/session";
import { listSubscriptionRequests, listUsers, planColumnExists, subscriptionTableExists } from "@/lib/auth/users";
import { ok, panel } from "@/lib/panel";
import { usesPostgres } from "@/lib/env";

export const dynamic = "force-dynamic";

/**
 * Account management — the Super Admin's module.
 *
 * Create an account, reset any password, move an account between plans, enable
 * or disable it, and work the queue the public Subscribe page fills. Every
 * action here is one audited write: `audit_logs` says who moved which field of
 * which account, and never a password — there is no email transport in this
 * deployment, so the admin sets the password and hands it over personally.
 *
 * The page degrades honestly against migration 0013's two states: no plan
 * column means every account displays UNLIMITED (exactly what the grandfather
 * pass will write) and the plan buttons refuse; no request table means the
 * queue section says the Subscribe page has nowhere to land yet.
 */
export default async function AccountsPage() {
  const admin = await requireAdminPage();
  const [users, requests, plans, queue] = await Promise.all([
    panel(() => listUsers()),
    panel(() => listSubscriptionRequests()),
    panel(() => planColumnExists()),
    panel(() => subscriptionTableExists()),
  ]);
  if (!ok(users)) {
    return (
      <div className="space-y-4">
        <PageHeading title="Accounts" subtitle="Who can sign in, what they see, and what happens when they forget." />
        <ErrorNote title="Accounts unread" detail={users.error} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeading
        title="Accounts"
        subtitle="Create, reset, plan, enable — and the Subscribe queue. Passwords are set here and delivered by you; this app sends no email, so there is no reset link to fake."
        aside={
          <span className="chip border-line-2 bg-surface-2 text-ink-2">
            <span className="num">{(users.data ?? []).length}</span> account{(users.data ?? []).length === 1 ? "" : "s"}
          </span>
        }
      />

      <Panel title="Everything on this page writes an audit row" hint="actor, action, account — never a password">
        <p className="px-4 py-3 text-[12px] leading-snug text-ink-2">
          {usesPostgres()
            ? "The one rule worth repeating: a disabled account is signed out of every live session, and a password reset here revokes the target's sessions immediately — a change they did not ask for takes effect now, not at cookie expiry."
            : "This copy of the dashboard has no database, so there are no accounts to manage. Point DATABASE_URL at the session pooler."}
        </p>
      </Panel>

      {usesPostgres() ? (
        <AccountsBoard
          users={users.data ?? []}
          requests={ok(requests) ? (requests.data ?? []) : []}
          plansLive={ok(plans) ? plans.data === true : false}
          queueLive={ok(queue) ? queue.data === true : false}
          selfId={admin.id}
        />
      ) : null}
    </div>
  );
}
