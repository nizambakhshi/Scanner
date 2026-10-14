"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * Change the signed-in operator's password.
 *
 * Three fields on purpose. The current password is required even though the
 * session cookie already proves who this is: a password change is also the
 * "someone else had my laptop" action, and making them know the thing they are
 * replacing is what stops a stolen cookie becoming ownership.
 *
 * The server response carries the number of other sessions it revoked, and the
 * success line repeats it — the operator should learn that their phone was signed
 * out without having to go and check.
 */
type Payload = { ok?: true; revokedSessions?: number; error?: string };

const INPUT =
  "num mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-[14px] text-ink outline-none transition focus:border-accent";

export function PasswordForm({ canChange }: { canChange: boolean }) {
  const router = useRouter();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  // Local only as a courtesy: the server re-checks everything, and a browser
  // cannot know the stored policy for the words already in use.
  const mismatch = confirm.length > 0 && confirm !== next;

  if (!canChange) {
    return (
      <div className="space-y-3 px-4 py-3 text-[13px] leading-snug text-ink-2">
        <p>
          This copy has no <span className="num">DATABASE_URL</span>, so there is no user table to write a new password
          to. Sign-in is verified against <span className="num">AUTH_ADMIN_PASSWORD_HASH</span> in the environment file
          instead.
        </p>
        <p className="text-[12px] text-muted">
          To change it: run <span className="num">npm run hash-password</span>, then put the printed hash into that
          variable and restart. The form on this page exists for the database-backed copy.
        </p>
      </div>
    );
  }

  return (
    <form
      className="space-y-3 px-4 py-3"
      onSubmit={async (event) => {
        event.preventDefault();
        if (mismatch) {
          setNote({ tone: "error", text: "The two new passwords do not match." });
          return;
        }
        setBusy(true);
        setNote(null);
        try {
          const response = await fetch("/api/password", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ currentPassword: current, newPassword: next, confirmPassword: confirm }),
          });
          const payload = (await response.json()) as Payload;
          if (!response.ok || payload.ok !== true) {
            setNote({ tone: "error", text: payload.error ?? `Could not change the password (HTTP ${response.status}).` });
            return;
          }
          setCurrent("");
          setNext("");
          setConfirm("");
          setNote({
            tone: "ok",
            text:
              payload.revokedSessions && payload.revokedSessions > 0
                ? `Password changed, and ${payload.revokedSessions} other session${payload.revokedSessions === 1 ? "" : "s"} signed out.`
                : "Password changed. No other session was signed in.",
          });
          // "Last sign-in" and the live-session count on this page come from the
          // server, so they need a refresh to agree with what just happened.
          router.refresh();
        } catch {
          setNote({ tone: "error", text: "Could not reach the dashboard API." });
        } finally {
          setBusy(false);
        }
      }}
    >
      <div>
        <label htmlFor="currentPassword" className="eyebrow">
          Current password
        </label>
        <input
          id="currentPassword"
          name="currentPassword"
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          className={INPUT}
        />
      </div>
      <div>
        <label htmlFor="newPassword" className="eyebrow">
          New password
        </label>
        <input
          id="newPassword"
          name="newPassword"
          type="password"
          autoComplete="new-password"
          required
          value={next}
          onChange={(event) => setNext(event.target.value)}
          className={INPUT}
        />
        <p className="mt-1 text-[11px] leading-snug text-muted">
          At least 10 characters. Not repetitive, not a common password, and not the one you are using now.
        </p>
      </div>
      <div>
        <label htmlFor="confirmPassword" className="eyebrow">
          Retype the new password
        </label>
        <input
          id="confirmPassword"
          name="confirmPassword"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          aria-invalid={mismatch || undefined}
          className={`${INPUT}${mismatch ? " border-negative" : ""}`}
        />
        {mismatch ? <p className="mt-1 text-[11px] text-negative">These two entries are not the same.</p> : null}
      </div>

      {note ? (
        <p
          role={note.tone === "error" ? "alert" : "status"}
          className={`border-l-2 px-3 py-2 text-[12px] ${
            note.tone === "ok"
              ? "border-positive bg-positive/5 text-positive"
              : "border-negative bg-negative/5 text-negative"
          }`}
        >
          {note.text}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={busy || current.length === 0 || next.length === 0 || confirm.length === 0}
        className="rounded-md bg-ink px-3 py-2 text-[14px] font-semibold text-surface transition hover:bg-ink-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? "Saving…" : "Change password"}
      </button>
    </form>
  );
}
