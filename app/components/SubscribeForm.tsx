"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * The public ask. It sends email, plan and an optional note to `/api/subscribe`
 * and shows whatever answer comes back — the queue row, the "already asked"
 * note, or the "not open yet" refusal, all in the server's own words so this
 * form can never promise something the database does not store.
 */
export function SubscribeForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [plan, setPlan] = useState("UNLIMITED");
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<{ kind: "idle" | "busy" | "error" | "done"; message: string }>({
    kind: "idle",
    message: "",
  });

  const busy = status.kind === "busy";

  return (
    <form
      className="space-y-3"
      onSubmit={async (event) => {
        event.preventDefault();
        setStatus({ kind: "busy", message: "" });
        let message = "";
        let okResponse = false;
        try {
          const response = await fetch("/api/subscribe", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ email, plan, note }),
          });
          const payload = (await response.json()) as { error?: string; note?: string };
          message = payload.error ?? payload.note ?? `Subscribe failed (${response.status}).`;
          okResponse = response.ok;
        } catch {
          message = "Could not reach the dashboard.";
        }
        setStatus({ kind: okResponse ? "done" : "error", message });
        if (okResponse) {
          setEmail("");
          setNote("");
          // The Accounts page is where a Super Admin reads this queue; a signed-in
          // operator watching the public page benefit from a refresh that shows
          // nothing stale, and everyone else just got a fresh row in a table.
          router.refresh();
        }
      }}
    >
      <div>
        <label htmlFor="sub-email" className="eyebrow">
          Email
        </label>
        <input
          id="sub-email"
          name="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="num mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-[14px] text-ink outline-none transition focus:border-accent"
        />
      </div>
      <div>
        <span className="eyebrow">Plan</span>
        <div className="mt-1 flex gap-2">
          {(["BASIC", "UNLIMITED"] as const).map((candidate) => (
            <button
              key={candidate}
              type="button"
              onClick={() => setPlan(candidate)}
              aria-pressed={plan === candidate}
              className={`flex-1 rounded-md border px-3 py-2 text-[13px] font-medium transition ${
                plan === candidate
                  ? "border-accent bg-amber/10 text-ink"
                  : "border-line text-muted hover:border-line-2 hover:text-ink-2"
              }`}
            >
              {candidate === "BASIC" ? "Basic — top 3 a day" : "Unlimited — full list"}
            </button>
          ))}
        </div>
      </div>
      <div>
        <label htmlFor="sub-note" className="eyebrow">
          Note for the operator <span className="normal-case tracking-normal">(optional)</span>
        </label>
        <textarea
          id="sub-note"
          name="note"
          rows={2}
          maxLength={500}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="What you would use the scanner for."
          className="mt-1 w-full resize-y rounded-md border border-line bg-surface px-3 py-2 text-[13px] leading-snug text-ink outline-none transition focus:border-accent"
        />
      </div>
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-md bg-ink px-3 py-2 text-[14px] font-semibold text-surface transition hover:bg-ink-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? "Sending…" : "Request access"}
      </button>
      {status.kind === "error" ? <p className="text-[12px] leading-snug text-negative">{status.message}</p> : null}
      {status.kind === "done" ? <p className="text-[12px] leading-snug text-positive">{status.message}</p> : null}
    </form>
  );
}
