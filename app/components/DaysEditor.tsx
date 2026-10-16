"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * The days the scanner operates — the weekday rule plus the operator's
 * exceptions.
 *
 * Exchange holidays are published nowhere this project can read, so the page
 * does not pretend to know the calendar: it asks for the two lists that say
 * otherwise. A **skip** is a weekday you do not want the scanner running on
 * (a holiday, a half day, a day you are away). A **force open** is a Saturday
 * or Sunday you do. Everything else stays Mon–Fri.
 *
 * Both lists are posted **complete** on every save — the same contract the
 * Telegram channel list uses — because a list you cannot see is a list you
 * cannot delete from, so the rows are plain text with a Delete button each.
 * The preview strip answers the question a saved calendar actually raises:
 * "so which days will it run?"
 */

type DayEntry = { date: string; note: string | null };
type DaysDoc = { version: 1; off_days: DayEntry[]; on_days: DayEntry[] };

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Tue · 26 Oct 2026" — the weekday spelled out is the check that the date is real. */
function pretty(iso: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const [y, m, d] = iso.split("-").map(Number);
  const day = new Date(Date.UTC(y, m - 1, d));
  return `${WEEKDAYS[day.getUTCDay()]} · ${d} ${MONTHS[m - 1]} ${y}`;
}

export function DaysEditor({ doc, readable }: { doc: DaysDoc; readable: boolean }) {
  const router = useRouter();
  const [offDays, setOffDays] = useState<DayEntry[]>(doc.off_days.map((day) => ({ ...day })));
  const [onDays, setOnDays] = useState<DayEntry[]>(doc.on_days.map((day) => ({ ...day })));
  const [newOff, setNewOff] = useState("");
  const [newOn, setNewOn] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  // The preview reflects the *edited* lists, not the stored row: the operator
  // should see the week their unsaved clicks would produce.
  const preview = upcoming({ version: 1, off_days: offDays, on_days: onDays }, 8);

  function add(list: "off" | "on") {
    const iso = list === "off" ? newOff : newOn;
    const setter = list === "off" ? setOffDays : setOnDays;
    const setOther = list === "off" ? setOnDays : setOffDays;
    const otherList = list === "off" ? onDays : offDays;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
      setNote({ tone: "error", text: "Pick a date first — the field above the Add button." });
      return;
    }
    if (otherList.some((day) => day.date === iso)) {
      setNote({ tone: "error", text: `${iso} is already on the other list. A day cannot be both skipped and forced open.` });
      return;
    }
    setNote(null);
    // Editing one side clears a stored contradiction on this side rather than
    // keeping two rows about the same date.
    setter((current) => dedupe([...current.filter((day) => day.date !== iso), { date: iso, note: null }]));
    setOther((current) => current.filter((day) => day.date !== iso));
    if (list === "off") setNewOff(""); else setNewOn("");
  }

  async function save() {
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/days", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // Lists travel as JSON strings because the API's body reader stringifies
        // every field; the route parses them back.
        body: JSON.stringify({
          off_days: JSON.stringify(offDays.map((day) => ({ date: day.date, note: day.note }))),
          on_days: JSON.stringify(onDays.map((day) => ({ date: day.date, note: day.note }))),
        }),
      });
      const payload = (await response.json()) as { note?: string; error?: string };
      setNote({ tone: response.ok && !payload.error ? "ok" : "error", text: payload.note ?? payload.error ?? `HTTP ${response.status}` });
      router.refresh();
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  async function resetToWeekdays() {
    setOffDays([]);
    setOnDays([]);
    setNote(null);
  }

  return (
    <div className="space-y-3 px-4 py-3">
      {!readable ? (
        <p className="text-[12px] text-amber">
          The saved row could not be read by this build, so the scanner is running the plain weekday rule. Saving below
          replaces that row.
        </p>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        <ExceptionList
          title="Skip these weekdays"
          hint="Holidays, half days, days off. The scanner does not start a session on them."
          entries={offDays}
          value={newOff}
          onChange={setNewOff}
          onAdd={() => add("off")}
          onRemove={(index) => setOffDays((list) => list.filter((_, i) => i !== index))}
        />
        <ExceptionList
          title="Force open weekends"
          hint="Rare, but a Saturday special session runs if it is listed here."
          entries={onDays}
          value={newOn}
          onChange={setNewOn}
          onAdd={() => add("on")}
          onRemove={(index) => setOnDays((list) => list.filter((_, i) => i !== index))}
        />
      </div>

      <div>
        <p className="eyebrow mb-1">Next days the scanner will run</p>
        <div className="flex flex-wrap gap-1.5">
          {preview.map((iso) => (
            <span key={iso} className="chip border-line-2 bg-surface-2 text-ink-2" title={pretty(iso)}>
              <span className="num">{iso}</span>
            </span>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="rounded border border-positive/40 bg-positive/10 px-3 py-1.5 text-[13px] font-semibold text-positive disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save calendar"}
        </button>
        {offDays.length + onDays.length > 0 ? (
          <button
            type="button"
            onClick={() => void resetToWeekdays()}
            disabled={busy}
            className="rounded border border-line-2 bg-surface-2 px-3 py-1.5 text-[13px] text-ink-2 hover:border-negative/50 hover:text-negative disabled:opacity-50"
          >
            Clear all exceptions
          </button>
        ) : null}
      </div>

      {note ? <p className={`text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p> : null}
    </div>
  );
}

function ExceptionList({
  title,
  hint,
  entries,
  value,
  onChange,
  onAdd,
  onRemove,
}: {
  title: string;
  hint: string;
  entries: DayEntry[];
  value: string;
  onChange: (iso: string) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div className="space-y-2">
      <p className="eyebrow">{title}</p>
      <p className="text-[11px] leading-snug text-muted">{hint}</p>
      <div className="flex items-center gap-2">
        <input
          type="date"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-label={`${title} — date to add`}
          className="num rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
        />
        <button
          type="button"
          onClick={onAdd}
          className="rounded border border-line-2 bg-surface-2 px-2 py-1 text-[12px] font-semibold text-ink hover:border-accent"
        >
          Add
        </button>
      </div>
      {entries.length === 0 ? (
        <p className="text-[12px] text-muted">None — the weekday rule stands.</p>
      ) : (
        <ul className="space-y-1">
          {entries.map((entry, index) => (
            <li key={entry.date} className="flex items-center gap-2 rounded border border-line bg-surface-2 px-2 py-1">
              <span className="num text-[12px] text-ink" title={pretty(entry.date)}>
                {entry.date}
              </span>
              <span className="text-[11px] text-muted">{pretty(entry.date)}</span>
              <button
                type="button"
                onClick={() => onRemove(index)}
                aria-label={`Remove ${entry.date} from ${title}`}
                className="ml-auto rounded border border-negative/40 px-2 py-0.5 text-[11px] text-negative hover:bg-negative/10"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Newest add wins over an older row for the same date, and the list stays sorted. */
function dedupe(list: DayEntry[]): DayEntry[] {
  const byDate = new Map<string, DayEntry>();
  for (const entry of list) byDate.set(entry.date, entry);
  return [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
}

/**
 * The next `count` dates the gate would let through, from today (IST) forward.
 * Local copy of the rule so the preview follows the in-progress edits; the
 * server's `upcomingTradingDays` in `lib/days.ts` is the same arithmetic — keep
 * the two in sync if the gate rule changes. IST is a fixed +05:30, so the shift
 * is plain epoch math and no browser locale is involved.
 */
function upcoming(doc: DaysDoc, count: number): string[] {
  const DAY_MS = 86_400_000;
  const istDay = Math.floor((Date.now() + 330 * 60_000) / DAY_MS);
  const days: string[] = [];
  for (let ahead = 0; days.length < count && ahead < 370; ahead += 1) {
    const iso = new Date((istDay + ahead) * DAY_MS).toISOString().slice(0, 10);
    const weekday = new Date(iso + "T00:00:00Z").getUTCDay();
    const forced = doc.on_days.some((entry) => entry.date === iso);
    const skipped = doc.off_days.some((entry) => entry.date === iso);
    const weekend = weekday === 0 || weekday === 6;
    if (forced || (!weekend && !skipped)) days.push(iso);
  }
  return days;
}
