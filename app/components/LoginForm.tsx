"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { isAdminRole } from "@/lib/roles";

type Response = { error?: string; setupProblem?: boolean } | { ok?: boolean; user?: { role?: string } };

export function LoginForm({ setupProblem }: { setupProblem: string | null }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<{ kind: "idle" | "busy" | "error"; message: string | null }>({
    kind: "idle",
    message: null,
  });

  return (
    <form
      className="space-y-3"
      onSubmit={async (event) => {
        event.preventDefault();
        setStatus({ kind: "busy", message: null });
        let payload: Response | null = null;
        try {
          const response = await fetch("/api/session", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ email, password }),
          });
          payload = (await response.json()) as Response;
          if (!response.ok) {
            const message = (payload as { error?: string }).error ?? `Sign-in failed (${response.status}).`;
            setStatus({ kind: "error", message });
            return;
          }
        } catch {
          setStatus({ kind: "error", message: "Could not reach the dashboard API." });
          return;
        }
        // The cookie is what authorises every page; without refreshing the server
        // cache the board would still render as signed out. A plain user lands on
        // their product — the Watchlist — while the operator's Board stays home.
        const role = (payload as { user?: { role?: string } }).user?.role ?? "";
        router.replace(role && !isAdminRole(role) ? "/watchlist" : "/");
        router.refresh();
      }}
    >
      <div>
        <label htmlFor="email" className="eyebrow">
          Email
        </label>
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="num mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-[14px] text-ink outline-none transition focus:border-accent"
        />
      </div>
      <div>
        <label htmlFor="password" className="eyebrow">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="num mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-[14px] text-ink outline-none transition focus:border-accent"
        />
      </div>

      {status.kind === "error" && status.message ? (
        <p role="alert" className="border-l-2 border-negative bg-negative/5 px-3 py-2 text-[12px] text-negative">
          {status.message}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={status.kind === "busy" || setupProblem !== null}
        className="w-full rounded-md bg-ink px-3 py-2 text-[14px] font-semibold text-surface transition hover:bg-ink-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {status.kind === "busy" ? "Checking…" : "Sign in"}
      </button>

      {setupProblem ? (
        <p className="text-[12px] leading-snug text-amber">
          Sign-in cannot work yet: {setupProblem} Set the variables listed on the deployment page, then restart.
        </p>
      ) : null}
    </form>
  );
}
