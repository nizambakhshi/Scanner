import Link from "next/link";
import { Panel, ErrorNote, PageHeading } from "@/components/ui";
import { SignalsTable } from "@/components/SignalsTable";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";
import { dateParam } from "@/lib/rows";
import type { SignalType } from "@/lib/types";

export const dynamic = "force-dynamic";

const TYPES: SignalType[] = [
  "BULLISH_ENTRY",
  "WATCHLIST_ADDED",
  "WATCHLIST_WEAKENING",
  "WATCHLIST_REMOVED",
  "WATCHLIST_REQUALIFIED",
  "DATA_FEED_INTERRUPTED",
  "DATA_FEED_RECOVERED",
  "DAILY_SCAN_COMPLETE",
  "RSI_BULLISH",
  "RSI_BEARISH",
];

const LIMITS = [25, 50, 100, 250];

/**
 * The alert feed, with the delivery queue attached to each row.
 *
 * The two are read together on purpose. A signal row is written even when its
 * Telegram delivery is suppressed by the cooldown or fails permanently, so a feed
 * without delivery state silently claims you were told about everything.
 */
export default async function SignalsPage({
  searchParams,
}: {
  searchParams: Promise<{ date?: string; type?: string; symbol?: string; limit?: string }>;
}) {
  const params = await searchParams;
  const date = dateParam(params.date);
  const rawType = (params.type ?? "").toUpperCase();
  const type = TYPES.includes(rawType as SignalType) ? rawType : "";
  const symbol = (params.symbol ?? "").trim().slice(0, 32);
  const limitChoice = LIMITS.includes(Number(params.limit)) ? Number(params.limit) : 100;

  // Sequential by necessity: the delivery rows are keyed by the signal ids the
  // first read returns. A failed delivery read costs the badges, not the feed.
  const signals = await panel(() =>
    source().signals({ tradingDate: date, signalType: type || null, symbol: symbol || null, limit: limitChoice }),
  );
  const rows = ok(signals) ? signals.data : [];
  const deliveries = await panel(() => source().deliveries(rows.map((row) => row.id)));

  const entries = new Set(rows.map((row) => row.signal_type));
  const telegramFailures = rows.filter((row) => row.telegram_failed).length;
  const suppressed = rows.filter((row) => row.telegram_status === "SUPPRESSED").length;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Alert feed"
        subtitle={
          <>
            Every signal the scanner raised, newest first, including the feed-interruption and end-of-day rows that are
            not trade setups but are still things an operator has to know.
          </>
        }
        aside={
          <form method="get" className="flex flex-wrap items-center gap-2">
            <Field label="Date">
              <input type="date" name="date" defaultValue={date ?? ""} className="input num" />
            </Field>
            <Field label="Type">
              <select name="type" defaultValue={type} className="input">
                <option value="">Any</option>
                {TYPES.map((value) => (
                  <option key={value} value={value}>
                    {value.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Symbol">
              <input
                type="search"
                name="symbol"
                defaultValue={symbol}
                placeholder="ANY"
                className="input num w-[9ch]"
                pattern="[A-Za-z0-9._-]{1,32}"
              />
            </Field>
            <Field label="Rows">
              <select name="limit" defaultValue={String(limitChoice)} className="input">
                {LIMITS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </Field>
            <button type="submit" className="rounded-md bg-ink px-3 py-1.5 text-[12px] font-semibold text-white">
              Apply
            </button>
            {date || type || symbol ? (
              <Link href="/signals" className="link text-[12px]">
                Clear
              </Link>
            ) : null}
          </form>
        }
      />

      <div className="flex flex-wrap gap-2 text-[12px]">
        <Tally label="Rows" value={rows.length} />
        <Tally label="Telegram failed" value={telegramFailures} tone={telegramFailures > 0 ? "text-negative" : "text-muted"} />
        <Tally label="Suppressed by cooldown" value={suppressed} tone="text-muted" />
        <Tally label="Types present" value={entries.size} />
      </div>

      <Panel title="Signals" hint={ok(signals) ? `read from the ${source().name} source` : undefined}>
        {ok(signals) ? (
          <SignalsTable signals={rows} deliveries={ok(deliveries) ? deliveries.data : []} />
        ) : (
          <ErrorNote title="Signal feed unavailable" detail={signals.error} />
        )}
      </Panel>

      {ok(signals) && rows.length === 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          No rows for this filter. If the date is a day the worker did not run, there is genuinely nothing to show; if
          you expected an alert and see nothing here, the signal was never raised — check the watchlist and the health
          page before assuming the notification was lost.
        </p>
      ) : null}

      {ok(deliveries) === false ? (
        <div className="px-1">
          <ErrorNote title="Delivery states unread" detail={deliveries.error} />
        </div>
      ) : null}
    </div>
  );
}

function Tally({ label, value, tone = "text-ink-2" }: { label: string; value: number; tone?: string }) {
  return (
    <span className="chip border-line bg-surface text-muted">
      {label} <span className={`num font-bold ${tone}`}>{value}</span>
    </span>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex items-center gap-1.5 text-[12px] text-muted">
      {label}
      {children}
    </label>
  );
}
