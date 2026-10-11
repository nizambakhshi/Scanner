import { Panel, ErrorNote, PageHeading } from "@/components/ui";
import { WatchlistTable } from "@/components/WatchlistTable";
import { longDate, orDash } from "@/lib/format";
import { ok, panel } from "@/lib/panel";
import { source } from "@/lib/source";
import { dateParam } from "@/lib/rows";

export const dynamic = "force-dynamic";

/**
 * The daily watchlist.
 *
 * `?date=YYYY-MM-DD` reads a past day; anything malformed is treated as "no
 * filter" by the data layer rather than reaching the database as a bad cast. The
 * view itself is the contract: a membership row is created only when a symbol
 * qualifies, so this table is the audit trail of what the scanner considered
 * tradeable, including what it later dropped.
 */
export default async function WatchlistPage({ searchParams }: { searchParams: Promise<{ date?: string }> }) {
  const { date } = await searchParams;
  const wanted = dateParam(date);
  const [rows, days] = await Promise.all([
    panel(() => source().currentWatchlist(wanted)),
    panel(() => source().history(30)),
  ]);

  const list = ok(rows) ? rows.data : [];
  const tracked = list.filter((row) => row.current_state === "ACTIVE" || row.current_state === "REQUALIFIED");
  const dropped = list.filter((row) => row.current_state === "REMOVED");
  const weakened = list.filter((row) => row.current_state === "WEAKENING" || row.current_state === "DATA_STALE");

  return (
    <div className="space-y-4">
      <PageHeading
        title="Daily watchlist"
        subtitle={
          <>
            Every symbol that ever qualified today, with the state it is in now. Removals stay here — an instrument
            that left the list is a decision the scanner made, and it should be reviewable.
          </>
        }
        aside={<DateFilter value={wanted ?? ""} days={ok(days) ? days.data.map((day) => day.trading_date) : []} />}
      />

      <div className="flex flex-wrap gap-2 text-[12px]">
        <Tally label="Tracked" value={tracked.length} tone="text-positive" />
        <Tally label="Weakening / stale" value={weakened.length} tone="text-amber" />
        <Tally label="Removed" value={dropped.length} tone="text-muted" />
        <Tally label="Total memberships" value={list.length} tone="text-ink-2" />
      </div>

      <Panel title={wanted ? `Memberships on ${longDate(wanted)}` : "Latest session"} hint="tracked first, then by score">
        {ok(rows) ? <WatchlistTable rows={list} /> : <ErrorNote title="Watchlist unavailable" detail={rows.error} />}
      </Panel>

      {ok(rows) && list.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          {orDash(wanted) === "—"
            ? "Unfiltered: the view returns the most recent session's memberships."
            : `Filtered to ${wanted}.`}{" "}
          A dash in an indicator column means the pipeline had not computed that value yet, not that it measured zero.
        </p>
      ) : null}
    </div>
  );
}

function Tally({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <span className="chip border-line bg-surface text-muted">
      {label} <span className={`num font-bold ${tone}`}>{value}</span>
    </span>
  );
}

/**
 * A plain GET form: the filter lives in the URL, so a filtered view is linkable and
 * a page refresh keeps the same day on screen.
 */
function DateFilter({ value, days }: { value: string; days: string[] }) {
  return (
    <form method="get" className="flex items-center gap-2">
      <label htmlFor="date" className="text-[12px] text-muted">
        Trading date
      </label>
      <input
        id="date"
        name="date"
        type="date"
        defaultValue={value}
        list="known-dates"
        className="num rounded-md border border-line bg-surface px-2 py-1 text-[12px]"
      />
      <datalist id="known-dates">
        {days.map((day) => (
          <option key={day} value={day} />
        ))}
      </datalist>
      <button type="submit" className="rounded-md border border-line px-2.5 py-1 text-[12px] font-medium hover:bg-surface-2">
        Show
      </button>
      {value ? (
        <a href="/watchlist" className="link text-[12px]">
          Latest
        </a>
      ) : null}
    </form>
  );
}
