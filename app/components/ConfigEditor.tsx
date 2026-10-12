"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * The qualification editor: "Edit values" → seven numbers → save.
 *
 * A save inserts a new versioned `scanner_configs` row server-side and the
 * worker adopts it when it opens its next session — so the note after saving
 * names both versions: the one just written and the one the running session
 * (or the parked worker's next open) will actually use. Validation mirrors the
 * worker's own rules so an impossible config is a form error, not a session
 * that refuses to start.
 */

type ConfigRowLite = {
  config_version: number;
  fast_ema_period: number;
  slow_ema_period: number;
  volume_lookback: number;
  relative_volume_threshold: number;
  alert_cooldown_minutes: number;
  removal_confirm_candles: number;
  initial_scan_candles: number;
};

type FieldSpec = {
  key: keyof ConfigRowLite;
  label: string;
  step: string;
  min: number;
  hint: string;
};

const FIELDS: readonly FieldSpec[] = [
  { key: "fast_ema_period", label: "Fast EMA period", step: "1", min: 2, hint: "candles" },
  { key: "slow_ema_period", label: "Slow EMA period", step: "1", min: 3, hint: "must exceed fast" },
  { key: "volume_lookback", label: "Volume lookback", step: "1", min: 2, hint: "candles in the mean" },
  { key: "relative_volume_threshold", label: "Relative volume ≥", step: "0.05", min: 0, hint: "×" },
  { key: "removal_confirm_candles", label: "Removal confirmation", step: "1", min: 1, hint: "weak candles" },
  { key: "alert_cooldown_minutes", label: "Alert cooldown", step: "1", min: 0, hint: "minutes" },
  { key: "initial_scan_candles", label: "Initial scan after", step: "1", min: 1, hint: "candles" },
];

export function ConfigEditor({ row }: { row: ConfigRowLite }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((field) => [field.key, String(row[field.key])])),
  );
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  function edit() {
    setValues(Object.fromEntries(FIELDS.map((field) => [field.key, String(row[field.key])])));
    setNote(null);
    setOpen(true);
  }

  async function save() {
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      });
      const payload = (await response.json()) as { version: number; changes: Record<string, unknown> } | { error: string };
      if (!response.ok || "error" in payload) {
        setNote({ tone: "error", text: "error" in payload ? payload.error : `HTTP ${response.status}` });
        return;
      }
      if (Object.keys(payload.changes).length === 0) {
        setNote({ tone: "ok", text: `Values unchanged — still config v${payload.version}.` });
        setOpen(false);
        return;
      }
      setNote({
        tone: "ok",
        text: `Saved as config v${payload.version}. The worker adopts it when it opens its next session — the current values keep running until then.`,
      });
      setOpen(false);
      router.refresh();
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="border-t border-line px-4 py-3">
        <button
          type="button"
          onClick={edit}
          className="rounded border border-line-2 bg-surface-2 px-3 py-1.5 text-[13px] font-semibold text-ink"
        >
          Edit values
        </button>
        <p className="mt-2 max-w-[70ch] text-[11px] leading-snug text-muted">
          Saving writes a new versioned config row and journals the change. The worker adopts it when it opens its next
          session — the running session keeps the values it started with. Current row: <span className="num">v{row.config_version}</span>.
        </p>
        {note ? (
          <p className={`mt-2 text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="border-t border-line px-4 py-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {FIELDS.map((field) => (
          <label key={field.key} className="flex flex-col gap-1">
            <span className="eyebrow">{field.label}</span>
            <input
              type="number"
              step={field.step}
              min={field.min}
              value={values[field.key] ?? ""}
              onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
              className="num w-full rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
            />
            <span className="text-[10px] leading-snug text-muted">{field.hint}</span>
          </label>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="rounded border border-positive/40 bg-positive/10 px-3 py-1.5 text-[13px] font-semibold text-positive disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          disabled={busy}
          className="rounded border border-line px-3 py-1.5 text-[13px] text-ink-2 hover:bg-hover"
        >
          Cancel
        </button>
      </div>
      {note ? (
        <p className={`mt-2 text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p>
      ) : null}
    </div>
  );
}
