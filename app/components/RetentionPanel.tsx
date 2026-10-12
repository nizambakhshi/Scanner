"use client";

import { useEffect, useState } from "react";

/**
 * The retention control: an ageing-window field, an optional stock scope, and
 * the purge button.
 *
 * Saving `days` here is also what the worker's automatic purge reads, so there
 * is exactly one definition of "old". The button is a real DELETE with no undo,
 * hence the confirm; the response's per-table counts come from the DELETE
 * statements themselves and are journalled to `audit_logs` by the server.
 */

type RetentionState = {
  days: number;
  updatedAt: string | null;
  lastPurge: {
    at: string | null;
    actor: string | null;
    days: number | null;
    symbol: string | null;
    deleted: Record<string, number> | null;
  } | null;
};

export function RetentionPanel() {
  const [state, setState] = useState<RetentionState | null>(null);
  const [days, setDays] = useState<string>("7");
  const [symbol, setSymbol] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/ops/retention", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = (await response.json()) as RetentionState;
        if (cancelled) return;
        setState(payload);
        setDays(String(payload.days));
      } catch {
        if (!cancelled) setNote({ tone: "error", text: "Could not read the saved retention window." });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function purge() {
    const parsed = Math.trunc(Number(days));
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 30) {
      setNote({ tone: "error", text: "Days must be an integer between 1 and 30." });
      return;
    }
    const scope = symbol.trim()
      ? `candles and indicator snapshots for ${symbol.trim().toUpperCase()} older than ${parsed} day(s)`
      : `ALL candles and indicator snapshots older than ${parsed} day(s)`;
    if (!window.confirm(`Delete ${scope}? There is no undo. The worker re-fetches history from the broker as needed.`)) {
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/ops/purge", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ days: parsed, symbol: symbol.trim() || undefined }),
      });
      const payload = (await response.json()) as
        | { days: number; symbol: string | null; deleted: Record<string, number> }
        | { error: string };
      if (!response.ok || "error" in payload) {
        setNote({ tone: "error", text: "error" in payload ? payload.error : `HTTP ${response.status}` });
        return;
      }
      const counts = payload.deleted;
      setNote({
        tone: "ok",
        text: `Purged ${counts.candles_1m ?? 0} candle(s) and ${counts.indicator_snapshots ?? 0} snapshot(s)${
          payload.symbol ? ` for ${payload.symbol}` : ""
        } older than ${payload.days} day(s). The window is saved.`,
      });
      const refreshed = await fetch("/api/ops/retention", { cache: "no-store" });
      if (refreshed.ok) setState((await refreshed.json()) as RetentionState);
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  const last = state?.lastPurge ?? null;

  return (
    <div className="px-4 py-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-wide text-muted">Ageing (days)</span>
          <input
            type="number"
            min={1}
            max={30}
            value={days}
            onChange={(event) => setDays(event.target.value)}
            className="num w-24 rounded border border-line bg-surface px-2 py-1.5 text-[13px]"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-wide text-muted">Stock (optional)</span>
          <input
            type="text"
            placeholder="ALL — every stock"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            className="num w-44 rounded border border-line bg-surface px-2 py-1.5 text-[13px] uppercase"
          />
        </label>
        <button
          type="button"
          onClick={() => void purge()}
          disabled={busy}
          className="rounded border border-negative/40 bg-negative/10 px-3 py-1.5 text-[13px] font-semibold text-negative disabled:opacity-50"
        >
          {busy ? "Purging…" : "Purge now"}
        </button>
      </div>
      <p className="mt-2 max-w-[70ch] text-[11px] leading-snug text-muted">
        Deletes completed 1-minute candles and indicator snapshots; watchlist, signal and delivery
        history is never touched. The saved window also drives the worker&rsquo;s automatic purge at
        session start and every 6 h. No undo — the counts below are what the DELETE reported.
      </p>
      {note ? (
        <p className={`mt-2 text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p>
      ) : null}
      <div className="mt-3 border-t border-line pt-2 text-[11px] text-muted">
        {last ? (
          <>
            Last purge: <span className="num">{last.at ?? "—"}</span>
            {last.actor ? <> by {last.actor}</> : null} ·{" "}
            <span className="num">{last.days ?? "?"}</span> day(s) ·{" "}
            {last.symbol && last.symbol !== "ALL" ? <span className="num">{last.symbol}</span> : "ALL"} ·{" "}
            {last.deleted
              ? Object.entries(last.deleted)
                  .map(([table, count]) => `${table.replace(/_/g, " ")} ${count}`)
                  .join(" · ")
              : "counts not recorded"}
          </>
        ) : state ? (
          <>No purge has been run from this dashboard yet. Saved window: {state.days} day(s).</>
        ) : (
          "Reading the saved window…"
        )}
      </div>
    </div>
  );
}
