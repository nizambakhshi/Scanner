"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * Signing out has to clear the server-side row too, not just the cookie, or the
 * session stays valid to anyone who captured it while a database is configured.
 */
export function SignOutButton({ everywhere = false }: { everywhere?: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      className="flex items-center gap-2"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const response = await fetch("/api/session", { method: "DELETE" });
          if (!response.ok) throw new Error(`Sign-out failed (${response.status}).`);
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "Sign-out failed.");
          setBusy(false);
          return;
        }
        router.replace("/login");
        router.refresh();
      }}
    >
      <button
        type="submit"
        disabled={busy}
        className="rounded-md border border-line px-2.5 py-1 text-[12px] font-medium text-ink-2 transition hover:border-line-2 hover:bg-hover disabled:opacity-60"
      >
        {busy ? "Signing out…" : everywhere ? "Sign out everywhere" : "Sign out"}
      </button>
      {error ? <span className="text-[12px] text-negative">{error}</span> : null}
    </form>
  );
}
