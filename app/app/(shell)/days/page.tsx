import Link from "next/link";
import { Panel, ErrorNote, PageHeading } from "@/components/ui";
import { DaysEditor } from "@/components/DaysEditor";
import { requireAdminPage } from "@/lib/auth/session";
import { ok, panel } from "@/lib/panel";
import { readDaysState } from "@/lib/alerts-store";
import { istToday } from "@/lib/market-clock";
import { dateTime } from "@/lib/format";
import { usesPostgres } from "@/lib/env";

export const dynamic = "force-dynamic";

/**
 * The days the scanner operates.
 *
 * The rule underneath is Monday–Friday, 09:15–15:30 IST — but the NSE calendar
 * is not published anywhere this project reads, so the exchange holidays, the
 * half days and the days you simply do not want a board lighting up are stated
 * here as exceptions instead of guessed by code. Saving writes one JSON
 * document (`ops_settings['days']`); the worker re-reads it within a minute and
 * the gate applies at the session-open decision — no restart, no redeploy.
 *
 * A day never started is a day with no watchlist, no alerts and no Telegram —
 * which is exactly what "skip" means, stated plainly so nobody mistakes a
 * holiday for a broken scanner.
 */
export default async function DaysPage() {
  await requireAdminPage();
  const days = await panel(() => readDaysState());
  const state = ok(days) ? days.data : null;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Days"
        subtitle={
          <>
            Weekdays run the scanner; this page is for the days that should not — and the rare weekend session that
            should. Today is <span className="num">{istToday(new Date())}</span>.
          </>
        }
        aside={
          state ? (
            <span
              className="chip border-line-2 bg-surface-2 text-ink-2"
              title={
                state.source === "saved"
                  ? "The row in ops_settings['days'] — what the worker's gate reads"
                  : "No usable exceptions row: the plain Monday–Friday rule runs"
              }
            >
              {state.source === "saved" ? "saved exceptions" : "weekday rule"}
            </span>
          ) : null
        }
      />

      {!ok(days) ? (
        <ErrorNote title="Days settings unread" detail={days.error} />
      ) : !state ? (
        <Panel title="Days">
          <p className="px-4 py-3 text-[12px] text-muted">The exceptions row could not be read.</p>
        </Panel>
      ) : (
        <Panel title="Exceptions" hint="lists are posted whole on every save">
          <DaysEditor doc={state.doc} readable={state.source === "saved" || state.updatedAt === null} />
        </Panel>
      )}

      {state?.lastChange || state?.updatedAt ? (
        <p className="px-1 text-[11px] leading-snug text-muted">
          Row updated <span className="num">{dateTime(state.updatedAt)}</span>
          {state.lastChange?.actor ? <> by {state.lastChange.actor}</> : null}
          {state.lastChange?.summary ? (
            <>
              {" — last write: "}
              <span className="num">{state.lastChange.summary}</span>
            </>
          ) : null}
          . Every save is one <span className="num">audit_logs</span> row with the dates that moved.
        </p>
      ) : null}

      <Panel title="What a skipped day does" hint="and what it does not">
        <ul className="grid gap-2 px-4 py-3 text-[12px] leading-snug text-ink-2 md:grid-cols-2">
          <li className="border-l-2 border-line-2 pl-3">
            The worker does not open a session: no watchlist, no signals, no alerts, no Telegram. The board stays on the
            last real day, and <Link href="/health" className="link">Health</Link> shows the heartbeat going quiet —
            that is the expected look of a holiday, not an outage.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            It does not delete anything. Candles, signals and history from earlier days are untouched, and the next
            weekday runs normally without a re-save.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            A date on both lists is refused at the door — a calendar that contradicts itself is a bug, not a precedence
            rule.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            The gate is read at the session-open decision, so a skip saved at 08:00 is honored at 09:15; one saved
            after the session opened takes effect the next morning.
          </li>
        </ul>
        {!usesPostgres() ? (
          <p className="border-t border-line px-4 py-3 text-[11px] leading-snug text-amber">
            This app is serving the replay sample, so nothing saved here would reach a real worker. Configure{" "}
            <span className="num">DATABASE_URL</span> to edit the calendar that runs.
          </p>
        ) : null}
      </Panel>
    </div>
  );
}
