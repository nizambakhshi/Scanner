import { Empty } from "./ui";
import { dateTime, jsonRecord, price } from "@/lib/format";
import type { EventRow } from "@/lib/types";

/**
 * The state machine, printed.
 *
 * `daily_watchlists` only keeps the current state; the transitions live in
 * `watchlist_events`. Without them an operator cannot tell a symbol that has been
 * quietly ACTIVE since 09:38 from one that has been added, removed and re-qualified
 * three times — and the second one is exactly the case that needs a closer look.
 */
const KIND: Record<string, { tone: string; label: string }> = {
  ADDED: { tone: "bg-positive", label: "Added" },
  STATE_CHANGE: { tone: "bg-accent", label: "State change" },
  WEAKENED: { tone: "bg-amber", label: "Weakened" },
  REMOVED: { tone: "bg-negative", label: "Removed" },
  REQUALIFIED: { tone: "bg-accent", label: "Requalified" },
  STALE: { tone: "bg-amber", label: "Stale data" },
};

export function EventTimeline({ events }: { events: EventRow[] }) {
  if (events.length === 0) {
    return <Empty>No watchlist transitions recorded.</Empty>;
  }
  return (
    <ol className="relative ml-1 border-l border-line pl-4">
      {events.map((event) => {
        const kind = KIND[event.event_type] ?? { tone: "bg-muted-2", label: event.event_type };
        const detail = summarise(event);
        return (
          <li key={event.id} className="relative py-2.5">
            <span className={`absolute -left-[21px] top-4 h-2 w-2 rounded-full ${kind.tone}`} aria-hidden />
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="num text-[12px] text-muted">{dateTime(event.event_timestamp)}</span>
              <span className="text-[12px] font-semibold">{kind.label}</span>
              {event.symbol ? <span className="num text-[12px] text-ink-2">{event.symbol}</span> : null}
              <span className="text-[12px] text-muted">
                {event.previous_state ?? "—"} → <span className="font-semibold text-ink-2">{event.new_state}</span>
              </span>
              {event.price !== null ? <span className="num text-[12px] text-ink-2">{price(event.price)}</span> : null}
            </div>
            {event.reason ? <p className="mt-0.5 text-[12px] leading-snug text-muted">{event.reason}</p> : null}
            {detail ? <p className="num mt-0.5 text-[11px] text-muted-2">{detail}</p> : null}
          </li>
        );
      })}
    </ol>
  );
}

/** `detail` is free-form JSON the pipeline writes for post-hoc reading; show the
 *  handful of keys that explain a transition instead of dumping the blob. */
function summarise(event: EventRow): string | null {
  const detail = jsonRecord(event.detail);
  const parts: string[] = [];
  for (const key of ["episode_count", "consecutive_weak_candles", "score", "reason_code", "stale_seconds"]) {
    if (detail[key] !== undefined && detail[key] !== null) parts.push(`${key}=${String(detail[key])}`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}
