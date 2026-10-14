"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  FIELD_LIMITS,
  PARAM_FIELDS,
  STRATEGIES,
  STRATEGY_COPY,
  formKey,
  type StrategiesDoc,
  type StrategyKey,
} from "@/lib/alert-strategies";

/**
 * The three watches, each written as the sentence the detector actually applies.
 *
 * The numbers live *inside* the rule rather than in a column beside it, because
 * the question on this page is never "what is threshold_pct" — it is "how far
 * past the day high, on how much volume, before my phone rings". Reading the line
 * tells you the configuration; typing in it changes the one thing you meant to
 * change. Every field the worker owns is still reachable: a strategy that takes
 * two candles to confirm says so in its own sentence.
 *
 * One Save for the board, because the document is one row: three separate writes
 * would be three audit entries and three chances for the worker's next poll to
 * read a half-saved set.
 */

type Activity = Partial<Record<StrategyKey, { up: number; down: number }>> | null;

type Editor = Record<string, string>;

function toEditor(doc: StrategiesDoc): Editor {
  const out: Editor = {};
  for (const key of STRATEGIES) {
    const params = doc.strategies[key];
    out[formKey(key, "enabled")] = params.enabled ? "true" : "false";
    out[formKey(key, "telegram")] = params.telegram ? "true" : "false";
    for (const field of PARAM_FIELDS) out[formKey(key, field)] = String(params[field]);
  }
  return out;
}

export function StrategyBoard({ doc, activity }: { doc: StrategiesDoc; activity: Activity }) {
  const router = useRouter();
  const [values, setValues] = useState<Editor>(() => toEditor(doc));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const set = (key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setNote(null);
  };

  async function save() {
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/strategies", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      });
      const payload = (await response.json()) as
        | { changes: Record<string, unknown>; note: string; warnings?: string[] }
        | { error: string };
      if (!response.ok || "error" in payload) {
        setNote({ tone: "error", text: "error" in payload ? payload.error : `HTTP ${response.status}` });
        return;
      }
      const moved = Object.keys(payload.changes ?? {}).length;
      setNote({
        tone: "ok",
        text: `${payload.note}${moved > 0 ? ` (${moved} field${moved > 1 ? "s" : ""} written)` : ""}`,
      });
      // The typed numbers are now the stored numbers, so they stay in the inputs;
      // the *server* half of the page (source chip, last-change line, today's
      // counts) is what needs re-reading.
      router.refresh();
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {STRATEGIES.map((key) => (
        <StrategyCard
          key={key}
          strategy={key}
          values={values}
          set={set}
          counts={activity ? activity[key] : undefined}
        />
      ))}

      <div className="card px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy}
            className="rounded border border-positive/40 bg-positive/10 px-3 py-1.5 text-[13px] font-semibold text-positive disabled:opacity-50"
          >
            {busy ? "Saving…" : "Save all three"}
          </button>
          <button
            type="button"
            onClick={() => {
              setValues(toEditor(doc));
              setNote(null);
            }}
            disabled={busy}
            className="rounded border border-line px-3 py-1.5 text-[13px] text-ink-2 hover:bg-hover"
          >
            Discard changes
          </button>
          <span className="text-[11px] leading-snug text-muted">
            One document, one write, one audit row. Nothing here needs a restart.
          </span>
        </div>
        {note ? (
          <p className={`mt-2 text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p>
        ) : null}
      </div>
    </div>
  );
}

function StrategyCard({
  strategy,
  values,
  set,
  counts,
}: {
  strategy: StrategyKey;
  values: Editor;
  set: (key: string, value: string) => void;
  counts?: { up: number; down: number };
}) {
  const copy = STRATEGY_COPY[strategy];
  const enabled = on(values[formKey(strategy, "enabled")]);
  const telegram = on(values[formKey(strategy, "telegram")]);

  return (
    <section className={`card ${enabled ? "" : "opacity-[0.72]"}`}>
      <div className="panel-title">
        <div className="flex items-baseline gap-2">
          <h2 className="font-display text-[12px] font-semibold uppercase tracking-[0.1em] text-ink">{copy.label}</h2>
          <span className="num text-[10px] uppercase tracking-[0.12em] text-muted-2">{strategy}</span>
        </div>
        <div className="flex items-center gap-2">
          {counts ? <TodayCounts counts={counts} /> : null}
          <Switch
            label={`${copy.label} armed`}
            on={enabled}
            onChange={(next) => set(formKey(strategy, "enabled"), next ? "true" : "false")}
          />
        </div>
      </div>

      <div className="px-4 py-4">
        <p className="max-w-[86ch] text-[15px] leading-[2.1] text-ink-2">
          {ruleLine(strategy, values, set)}
        </p>
        <p className="mt-3 max-w-[80ch] text-[11px] leading-snug text-muted">{copy.what}</p>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line px-4 py-3">
        <label className="flex items-center gap-2">
          <Switch
            label={`${copy.label} to Telegram`}
            on={telegram}
            onChange={(next) => set(formKey(strategy, "telegram"), next ? "true" : "false")}
          />
          <span className="text-[12px] font-medium text-ink-2">Page my phone</span>
        </label>
        <span className="max-w-[60ch] text-[11px] leading-snug text-muted">
          {telegram ? copy.telegramNote : "Off: the row still lands on the alert feed, it just never leaves the database."}
        </span>
        <span className="ml-auto text-[11px] text-muted-2">{copy.direction}</span>
      </div>
    </section>
  );
}

/**
 * The rule as a sentence with its own numbers in it. Each inline field keeps the
 * worker's range as `min`/`max`, so the browser refuses the impossible values
 * before the server does — and the server still checks them, because a browser is
 * not a security boundary.
 */
function ruleLine(strategy: StrategyKey, values: Editor, set: (key: string, value: string) => void) {
  const field = (name: (typeof PARAM_FIELDS)[number], width: number, unit: string) => (
    <InlineNumber
      strategy={strategy}
      field={name}
      width={width}
      unit={unit}
      value={values[formKey(strategy, name)] ?? ""}
      onChange={(next) => set(formKey(strategy, name), next)}
    />
  );

  if (strategy === "session_break") {
    return (
      <>
        When a minute <Emphasis>closes</Emphasis> {field("threshold_pct", 4, "%")} past the session{" "}
        <Emphasis>high or low</Emphasis> it already printed, held for {field("confirm_candles", 2, "candle(s)")} and
        carrying {field("min_volume_ratio", 4, "×")} relative volume — raise it, then stay quiet for{" "}
        {field("cooldown_minutes", 4, "min")}.
      </>
    );
  }
  if (strategy === "orb") {
    return (
      <>
        A five-minute bucket that <Emphasis>closes</Emphasis> {field("threshold_pct", 4, "%")} beyond the{" "}
        <Emphasis>opening range</Emphasis> of the first five minutes, on {field("min_volume_ratio", 4, "×")} the mean
        volume of every earlier bucket — the same arithmetic the{" "}
        <Link href="/strategy" className="link">
          strategy board
        </Link>{" "}
        shows, {field("confirm_candles", 2, "bucket(s)")} deep, cooldown {field("cooldown_minutes", 4, "min")}.
      </>
    );
  }
  return (
    <>
      A close {field("threshold_pct", 4, "%")} across the session <Emphasis>VWAP</Emphasis> that holds for{" "}
      {field("confirm_candles", 2, "candle(s)")} and carries {field("min_volume_ratio", 4, "×")} relative volume —
      reclaim or loss, at most one page per {field("cooldown_minutes", 4, "min")}.
    </>
  );
}

function Emphasis({ children }: { children: React.ReactNode }) {
  return <strong className="font-semibold text-ink">{children}</strong>;
}

/**
 * One of the rule's numbers, set into the sentence rather than a form grid.
 *
 * `width` is the call site's guess at the field's natural size; the box takes the
 * larger of that and the value actually stored, because "0.35" in a 4ch box used to
 * render as "0." and read as a broken scanner. `.num-inline` drops Chrome's spinner
 * arrows, which live inside the box and steal two of those characters.
 */
function InlineNumber({
  strategy,
  field,
  unit,
  width,
  value,
  onChange,
}: {
  strategy: StrategyKey;
  field: (typeof PARAM_FIELDS)[number];
  unit: string;
  width: number;
  value: string;
  onChange: (value: string) => void;
}) {
  const limits = FIELD_LIMITS[field];
  return (
    <span className="mx-0.5 inline-flex items-baseline gap-1 align-baseline">
      <input
        type="number"
        aria-label={`${STRATEGY_COPY[strategy].label} ${field.replace(/_/g, " ")}`}
        value={value}
        step={limits.step}
        min={limits.min}
        max={limits.max}
        onChange={(event) => onChange(event.target.value)}
        className="num num-inline rounded-sm border-0 border-b border-dashed border-line-2 bg-surface-2 px-1 text-center text-[15px] font-semibold text-ink focus:border-accent focus:outline-none"
        style={{ width: `${Math.max(width, value.length + 1)}ch` }}
      />
      <span className="text-[11px] text-muted-2">{unit}</span>
    </span>
  );
}

function on(value: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test((value ?? "").trim());
}

/** The on/off control, drawn as a sliding pair of states rather than a checkbox. */
function Switch({ label, on: isOn, onChange }: { label: string; on: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={isOn}
      aria-label={label}
      title={isOn ? "Armed" : "Off"}
      onClick={() => onChange(!isOn)}
      className={`relative h-[18px] w-[34px] shrink-0 rounded-full border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${
        isOn ? "border-positive/50 bg-positive/25" : "border-line-2 bg-surface-2"
      }`}
    >
      <span
        className={`absolute top-[2px] h-[12px] w-[12px] rounded-full ${
          isOn ? "left-[18px] bg-positive" : "left-[2px] bg-muted-2"
        }`}
        aria-hidden
      />
    </button>
  );
}

function TodayCounts({ counts }: { counts: { up: number; down: number } }) {
  const total = counts.up + counts.down;
  const href = `/signals?type=${total === 0 ? "" : counts.up >= counts.down ? "BREAKOUT_UP" : "BREAKDOWN"}`;
  return (
    <Link
      href={total === 0 ? "/signals" : href}
      title="Level breaks this strategy raised today — the rows it wrote, not the messages it sent"
      className="chip border-line bg-surface-2 text-muted hover:border-line-2 hover:text-ink-2"
    >
      today <span className="num text-positive">{counts.up}</span>
      <span className="text-muted-2">/</span>
      <span className="num text-negative">{counts.down}</span>
    </Link>
  );
}
