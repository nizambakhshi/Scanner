"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { dateTime } from "@/lib/format";

/**
 * Account management: the Super Admin's hands on `app_users` and on the
 * Subscribe page's queue.
 *
 * Every card here is one POST to `/api/accounts` behind the admin guard. The
 * rules themselves (roles, plans, password strength, email shape) are the
 * server's — `lib/accounts.ts` and `lib/auth/users.ts` — and this component
 * only ever shows what came back. Two states are spelled out rather than
 * hidden: migration 0013 not applied (no plan column, no request table — the
 * queue section says so), and a password that exists only in transit (this app
 * sends no email, so the admin hands it over personally; the field clears on a
 * successful save and the value never comes back).
 */

export type ManagedUserRow = {
  id: string;
  email: string;
  role: string;
  displayName: string | null;
  isActive: boolean;
  plan: "BASIC" | "UNLIMITED";
  lastLoginAt: string | null;
  createdAt: string;
};

export type RequestRow = {
  id: string;
  email: string;
  planRequested: "BASIC" | "UNLIMITED";
  note: string | null;
  status: "PENDING" | "APPROVED" | "DISMISSED";
  createdAt: string;
  handledAt: string | null;
  handledByEmail: string | null;
};

type Response = { ok?: boolean; note?: string; error?: string };

async function post(payload: Record<string, string>): Promise<Response> {
  const response = await fetch("/api/accounts", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return (await response.json().catch(() => ({}))) as Response;
}

export function AccountsBoard({
  users,
  requests,
  plansLive,
  queueLive,
  selfId,
}: {
  users: ManagedUserRow[];
  requests: RequestRow[];
  plansLive: boolean;
  queueLive: boolean;
  selfId: string;
}) {
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const router = useRouter();

  async function run(payload: Record<string, string>) {
    const result = await post(payload);
    setNote({ tone: result.ok ? "ok" : "error", text: result.note ?? result.error ?? "No answer from the server." });
    if (result.ok) router.refresh();
  }

  return (
    <div className="space-y-4">
      {note ? (
        <p role="status" className={`text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>
          {note.text}
        </p>
      ) : null}

      <CreateAccount onCreate={(payload) => void run(payload)} />

      <div>
        <p className="eyebrow mb-2">Accounts</p>
        <div className="overflow-x-auto rounded border border-line">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-line bg-surface-2 text-left text-[11px] uppercase tracking-wide text-muted">
                <th className="px-3 py-2 font-semibold">Email</th>
                <th className="px-3 py-2 font-semibold">Role</th>
                <th className="px-3 py-2 font-semibold">Plan</th>
                <th className="px-3 py-2 font-semibold">Active</th>
                <th className="px-3 py-2 font-semibold">Last login</th>
                <th className="px-3 py-2 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <UserRow key={user.id} user={user} plansLive={plansLive} isSelf={user.id === selfId} run={run} />
              ))}
            </tbody>
          </table>
        </div>
        {!plansLive ? (
          <p className="mt-2 text-[11px] leading-snug text-amber">
            The plan column is not in this database yet (migration 0013 is drafted, not applied), so every account shows
            UNLIMITED — which is exactly what the migration&rsquo;s grandfather pass will write. Plan buttons stay dead
            until it lands.
          </p>
        ) : null}
      </div>

      <div>
        <p className="eyebrow mb-2">Subscribe queue</p>
        {!queueLive ? (
          <p className="text-[12px] text-muted">
            No <span className="num">subscription_requests</span> table on this database yet — the public Subscribe
            page will say subscriptions are not open until migration 0013 is applied.
          </p>
        ) : requests.length === 0 ? (
          <p className="text-[12px] text-muted">Nothing waiting. A Subscribe press on the public page lands here.</p>
        ) : (
          <ul className="space-y-2">
            {requests.map((request) => (
              <RequestRowView
                key={request.id}
                request={request}
                run={(payload) => void run(payload)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function CreateAccount({ onCreate }: { onCreate: (payload: Record<string, string>) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("USER");
  const [plan, setPlan] = useState("BASIC");
  return (
    <div className="rounded border border-line bg-surface p-3">
      <p className="eyebrow mb-2">Add an account</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-muted">Email</span>
          <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="trader@example.com" className="num w-56 rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-muted">Password (hand it over yourself)</span>
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className="num w-48 rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-muted">Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)} className="rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px]">
            <option value="USER">USER</option>
            <option value="ADMIN">ADMIN</option>
            <option value="SUPER_ADMIN">SUPER_ADMIN</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-muted">Plan</span>
          <select value={plan} onChange={(e) => setPlan(e.target.value)} className="rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px]">
            <option value="BASIC">BASIC — top 3 a day</option>
            <option value="UNLIMITED">UNLIMITED</option>
          </select>
        </label>
        <button
          type="button"
          onClick={() => {
            onCreate({ action: "create", email, password, role, plan });
            setPassword("");
          }}
          disabled={email === "" || password === ""}
          className="rounded border border-positive/40 bg-positive/10 px-3 py-1.5 text-[13px] font-semibold text-positive disabled:opacity-50"
        >
          Create account
        </button>
      </div>
    </div>
  );
}

function UserRow({
  user,
  plansLive,
  isSelf,
  run,
}: {
  user: ManagedUserRow;
  plansLive: boolean;
  isSelf: boolean;
  run: (payload: Record<string, string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const otherPlan = user.plan === "BASIC" ? "UNLIMITED" : "BASIC";
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2">
        <span className="num text-ink">{user.email}</span>
        {isSelf ? <span className="ml-1 text-[10px] uppercase text-muted-2">you</span> : null}
      </td>
      <td className="px-3 py-2 text-ink-2">{user.role}</td>
      <td className="px-3 py-2"><span className={`chip ${user.plan === "UNLIMITED" ? "border-positive/40 bg-positive/10 text-positive" : "border-line-2 bg-surface-2 text-ink-2"}`}>{user.plan}</span></td>
      <td className="px-3 py-2 text-ink-2">{user.isActive ? "yes" : "no"}</td>
      <td className="num px-3 py-2 text-muted">{user.lastLoginAt ? dateTime(user.lastLoginAt) : "never"}</td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" onClick={() => setOpen((v) => !v)} className="rounded border border-line-2 px-2 py-0.5 text-[11px] text-ink-2 hover:border-accent">
            Reset password
          </button>
          <button
            type="button"
            disabled={!plansLive}
            title={plansLive ? `Switch to ${otherPlan}` : "Apply migration 0013 first"}
            onClick={() => run({ action: "set_plan", id: user.id, plan: otherPlan })}
            className="rounded border border-line-2 px-2 py-0.5 text-[11px] text-ink-2 hover:border-accent disabled:opacity-40"
          >
            → {otherPlan}
          </button>
          <button
            type="button"
            disabled={isSelf}
            title={isSelf ? "You cannot disable the account you are signed in with" : undefined}
            onClick={() => run({ action: "set_active", id: user.id, active: user.isActive ? "false" : "true" })}
            className={`rounded border px-2 py-0.5 text-[11px] disabled:opacity-40 ${user.isActive ? "border-negative/40 text-negative hover:bg-negative/10" : "border-positive/40 text-positive hover:bg-positive/10"}`}
          >
            {user.isActive ? "Disable" : "Enable"}
          </button>
        </div>
        {open ? (
          <div className="mt-1.5 flex items-center gap-1.5">
            <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="new password" className="num w-40 rounded border border-line-2 bg-surface px-2 py-1 text-[12px] focus:border-accent focus:outline-none" />
            <button
              type="button"
              disabled={password === ""}
              onClick={() => {
                run({ action: "reset_password", id: user.id, password });
                setPassword("");
                setOpen(false);
              }}
              className="rounded border border-positive/40 bg-positive/10 px-2 py-1 text-[11px] font-semibold text-positive disabled:opacity-50"
            >
              Set
            </button>
          </div>
        ) : null}
      </td>
    </tr>
  );
}

function RequestRowView({ request, run }: { request: RequestRow; run: (payload: Record<string, string>) => void }) {
  const [password, setPassword] = useState("");
  const pending = request.status === "PENDING";
  return (
    <li className="rounded border border-line bg-surface p-3 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="num text-ink">{request.email}</span>
        <span className="chip border-line-2 bg-surface-2 text-ink-2">asked {request.planRequested}</span>
        <span className={`chip ${request.status === "PENDING" ? "border-amber/40 bg-amber/10 text-amber" : request.status === "APPROVED" ? "border-positive/40 bg-positive/10 text-positive" : "border-line-2 text-muted"}`}>
          {request.status}
        </span>
        <span className="num text-[11px] text-muted">{dateTime(request.createdAt)}</span>
        {request.handledByEmail ? <span className="text-[11px] text-muted">by {request.handledByEmail}</span> : null}
      </div>
      {request.note ? <p className="mt-1 border-l-2 border-line-2 pl-2 text-[11px] leading-snug text-ink-2">{request.note}</p> : null}
      {pending ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password for the new account" className="num w-52 rounded border border-line-2 bg-surface px-2 py-1 text-[12px] focus:border-accent focus:outline-none" />
          <button
            type="button"
            disabled={password === ""}
            title="Creates the account with the requested plan — hand the password over yourself"
            onClick={() => {
              run({ action: "handle_request", id: request.id, decision: "APPROVE", password });
              setPassword("");
            }}
            className="rounded border border-positive/40 bg-positive/10 px-2 py-1 text-[11px] font-semibold text-positive disabled:opacity-50"
          >
            Approve + create
          </button>
          <button
            type="button"
            onClick={() => run({ action: "handle_request", id: request.id, decision: "DISMISS", password: "" })}
            className="rounded border border-negative/40 px-2 py-1 text-[11px] text-negative hover:bg-negative/10"
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </li>
  );
}
