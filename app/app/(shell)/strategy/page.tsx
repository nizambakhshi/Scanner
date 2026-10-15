import { Panel, ErrorNote, PageHeading, FilterChip, Empty } from "@/components/ui";
import { OrbTable } from "@/components/OrbTable";
import { filterOrbViews, parseOrbStatusFilter, type OrbStatusFilter } from "@/lib/filters";
import { longDate } from "@/lib/format";
import { compareOrb, deriveOrb } from "@/lib/orb";
import { ok, panel } from "@/lib/panel";
import { sectorReadsFrom } from "@/lib/sector-reads";
import { source } from "@/lib/source";
import type { ReactNode } from "react";

export const dynamic = "force-dynamic";

/** What an empty result under each filter means — per filter, so the message
 *  never confuses "no setups in this cut" with "no data at all". */
const ORB_FILTER_EMPTY: Record<Exclude<OrbStatusFilter, "ALL">, string> = {
  BROKE_UP: "No up-breakouts right now",
  BROKE_DOWN: "No down-breakouts right now",
  CONFIRMED: "No confirmed setups right now — no breakout passes all three filters",
};

/**
 * Strategy 1 — Opening Range Breakout (5-min ORB with VWAP filter), evaluated
 * against the stored 1-minute candles.
 *
 * Read-only by design: the board computes the range, the first 5-minute close
 * beyond it, and the three confirmation readings the strategy's filters name —
 * and it stops there. Placing orders is outside this system's charter, so the
 * page is a monitor, not an execution surface.
 */
export default async function StrategyPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const status = parseOrbStatusFilter(params.status);

  // Anchored like the watchlist: today's opening range, or nothing — the SQL
  // source returns no rows for a date without candles instead of falling back
  // to the newest session with data.
  const today = await source().activeTradingDate();
  const [orb, sectorQuotes] = await Promise.all([
    panel(() => source().orbStrategy(today)),
    panel(() => source().sectorQuotes()),
  ]);
  const rows = ok(orb) ? orb.data : [];
  const views = rows.map(deriveOrb).sort(compareOrb);
  const visible = filterOrbViews(views, status);
  // First paint for the chips; the shared poller keeps them current.
  const sectorReads = ok(sectorQuotes) ? sectorReadsFrom(sectorQuotes.data) : undefined;

  const brokeUp = views.filter((view) => view.status === "BROKE_UP").length;
  const brokeDown = views.filter((view) => view.status === "BROKE_DOWN").length;
  const confirmed = views.filter((view) => view.confirmed).length;
  const sessionDate = rows[0]?.session_date ?? today ?? null;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Strategy"
        subtitle={
          <>
            Opening Range Breakout: the first five minutes (9:15–9:20) set the day&rsquo;s reference range, and the first
            five-minute close beyond it — with VWAP, EMA and volume confirmation — is the signal. Read-only: this board
            evaluates and displays, the scanner never places orders.
          </>
        }
      />

      <StrategyRules />

      <div className="flex flex-wrap gap-2 text-[12px]">
        <FilterChip href="/strategy" label="In view" value={views.length} active={status === "ALL"} />
        <FilterChip href="/strategy?status=BROKE_UP" label="Broke up" value={brokeUp} tone="text-positive" active={status === "BROKE_UP"} />
        <FilterChip href="/strategy?status=BROKE_DOWN" label="Broke down" value={brokeDown} tone="text-negative" active={status === "BROKE_DOWN"} />
        <FilterChip
          href="/strategy?status=CONFIRMED"
          label="All filters pass"
          value={confirmed}
          tone={confirmed > 0 ? "text-positive" : "text-ink-2"}
          active={status === "CONFIRMED"}
        />
      </div>

      <Panel
        title="Opening range board"
        hint={
          ok(orb)
            ? `${sessionDate ? `session ${longDate(sessionDate)} · ` : ""}read from the ${source().name} source${
                status !== "ALL" ? ` · showing ${visible.length}` : ""
              }`
            : undefined
        }
      >
        {ok(orb) ? (
          views.length > 0 && visible.length === 0 && status !== "ALL" ? (
            <Empty>{ORB_FILTER_EMPTY[status]} — the counts above describe the full board.</Empty>
          ) : views.length === 0 ? (
            <Empty>
              {today
                ? `No opening-range readings for ${longDate(today)} yet — the board fills in as today's 1-minute candles arrive. A previous session's board is never shown here.`
                : "No session with candles to read."}
            </Empty>
          ) : (
            <OrbTable views={visible} sectorReads={sectorReads} />
          )
        ) : (
          <ErrorNote title="Strategy board unavailable" detail={orb.error} />
        )}
      </Panel>

      {ok(orb) && views.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          Every reading is computed from the session&rsquo;s stored 1-minute candles: the range from the 9:15–9:20 bars,
          breakouts from 5-minute closes, VWAP with the worker&rsquo;s own Σ(TP·V)/ΣV formula. EMA 9/20 comes from the
          newest worker snapshot — it carries across sessions, so it is a dash until the worker has scanned the symbol.
          A breakout shows as soon as the newest 5-minute candle closes beyond the range; during market hours that
          candle may still be forming. A dash means &ldquo;not produced yet&rdquo;, never zero.
        </p>
      ) : null}
    </div>
  );
}

function StrategyRules() {
  return (
    <Panel title="How the strategy reads">
      <div className="grid gap-4 px-4 py-3 lg:grid-cols-2">
        <Rule heading="The range">
          At 9:15 mark the high and low of the first five minutes (9:15–9:20 IST). That is the opening range — the
          &ldquo;battle zone&rdquo; where overnight news and pre-market orders are resolved.
        </Rule>
        <Rule heading="The signal">
          After 9:20, the first five-minute candle to <em>close</em> above the range high is the long trigger; a close
          below the range low is the short trigger. Inside the range, wait.
        </Rule>
        <Rule heading="Stop and targets">
          Stop at the opposite end of the range. Target 1 is 1× the range width from the broken edge, target 2 is 2× —
          then trail the rest.
        </Rule>
        <Rule heading="The three filters">
          Price above VWAP for longs (below for shorts); the 9/20 EMA agreeing with the direction; and the breakout
          candle trading at least 1.5× the session&rsquo;s average five-minute volume. All three passes make the setup
          &ldquo;confirmed&rdquo; in the tally above.
        </Rule>
      </div>
      <p className="border-t border-line px-4 py-2 text-[11px] leading-snug text-muted">
        The write-up this page follows reports a 55–65% win rate with the filters applied. That is the source&rsquo;s
        claim, not a result this scanner has measured — this board shows the live reading of the rules and nothing here
        is trading advice.
      </p>
    </Panel>
  );
}

function Rule({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <div>
      <p className="eyebrow">{heading}</p>
      <p className="mt-1 text-[12.5px] leading-snug text-ink-2">{children}</p>
    </div>
  );
}
