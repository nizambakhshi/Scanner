import Link from "next/link";
import { Panel, ErrorNote, Empty, PageHeading } from "@/components/ui";
import { StrategyBoard } from "@/components/StrategyBoard";
import { TelegramPanel } from "@/components/TelegramPanel";
import { ok, panel } from "@/lib/panel";
import { readActivityToday, readStrategiesState, readTelegramState } from "@/lib/alerts-store";
import { STRATEGIES, strategiesAdvisories } from "@/lib/alert-strategies";
import { usesPostgres } from "@/lib/env";
import { dateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

/**
 * The alert rules the running worker obeys.
 *
 * Three level detectors, each with its own threshold, volume evidence, cooldown
 * and phone-call switch, plus where the pages are sent. This page writes
 * `ops_settings`; the worker re-reads that row every 60 seconds and applies what
 * changed *inside the current session* — which is what makes it different from
 * `/config`, whose versioned row is only adopted when the next session opens.
 *
 * Both reads are independent panels: a database that will not answer about
 * strategies must still let the operator see and fix their Telegram destination,
 * and vice versa.
 */
export default async function AlertsPage() {
  const [strategies, telegram, activity] = await Promise.all([
    panel(() => readStrategiesState()),
    panel(() => readTelegramState()),
    panel(() => readActivityToday()),
  ]);

  const strategyState = ok(strategies) ? strategies.data : null;
  const telegramState = ok(telegram) ? telegram.data : null;
  const todayCounts = ok(activity) ? activity.data : null;
  const advisories = strategyState ? strategiesAdvisories(strategyState.doc) : [];
  const armed = strategyState ? STRATEGIES.filter((key) => strategyState.doc.strategies[key].enabled).length : 0;

  return (
    <div className="space-y-4">
      <PageHeading
        title="Alert rules"
        subtitle={
          <>
            What the scanner watches for on every stock in the F&amp;O universe, minute by minute, and where it tells
            you. Saving writes one JSON document; the worker re-reads it within a minute and applies it to the session
            already running — no restart, no redeploy.
          </>
        }
        aside={
          strategyState ? (
            <>
              <span
                className="chip border-line-2 bg-surface-2 text-ink-2"
                title={
                  strategyState.source === "saved"
                    ? "The row in ops_settings['alert_strategies'] — what the worker obeys"
                    : "No saved row: the thresholds the worker ships with are in force"
                }
              >
                {strategyState.source === "saved" ? "saved rules" : "built-in defaults"}
              </span>
              <span className="chip border-line bg-surface text-muted">
                <span className="num">{armed}</span> of 3 armed
              </span>
            </>
          ) : null
        }
      />

      {!ok(strategies) ? (
        <ErrorNote title="Alert rules unread" detail={strategies.error} />
      ) : !strategyState ? (
        <Panel title="Alert rules">
          <Empty>No strategy document could be read.</Empty>
        </Panel>
      ) : (
        <>
          {strategyState.problem ? (
            <Panel title="About the saved document" hint={strategyState.source === "saved" ? "and what it says" : "why the defaults are running"}>
              <p className="px-4 py-3 text-[12px] leading-snug text-amber">
                {strategyState.problem}
                {strategyState.source === "defaults"
                  ? " — so the shipped defaults are what the detectors obey. Fix the values below and save; the save replaces the document the page cannot obey."
                  : ""}
              </p>
            </Panel>
          ) : null}

          {advisories.length > 0 ? (
            <Panel title="Worth knowing before you save" hint="what these numbers will do">
              <ul className="grid gap-2 px-4 py-3 text-[12px] leading-snug text-ink-2">
                {advisories.map((note) => (
                  <li key={note} className="border-l-2 border-amber/50 pl-3">
                    {note}
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}

          <StrategyBoard doc={strategyState.doc} activity={todayCounts} />

          {strategyState.lastChange || strategyState.updatedAt ? (
            <p className="px-1 text-[11px] leading-snug text-muted">
              Row updated <span className="num">{dateTime(strategyState.updatedAt)}</span>
              {strategyState.lastChange?.actor ? <> by {strategyState.lastChange.actor}</> : null}
              {strategyState.lastChange?.summary ? (
                <>
                  {" — last write: "}
                  <span className="num">{strategyState.lastChange.summary}</span>
                </>
              ) : null}
              . Every save is one <span className="num">audit_logs</span> row with the field-level diff.
            </p>
          ) : null}
        </>
      )}

      {!ok(telegram) ? (
        <ErrorNote title="Telegram settings unread" detail={telegram.error} />
      ) : !telegramState ? (
        <Panel title="Telegram delivery">
          <Empty>The credential row could not be read.</Empty>
        </Panel>
      ) : (
        <Panel title="Telegram delivery" hint="the only message this app sends is the test you ask for">
          <TelegramPanel
            state={telegramState}
            lastChange={
              telegramState.lastChange
                ? `${telegramState.lastChange.at ? dateTime(telegramState.lastChange.at) : "unknown time"} by ${telegramState.lastChange.actor ?? "an operator"}`
                : null
            }
          />
        </Panel>
      )}

      <Panel title="How a save reaches the phone" hint="worker/worker/main.py">
        <ol className="grid gap-3 px-4 py-3 text-[12px] leading-snug text-ink-2 md:grid-cols-2">
          <li className="border-l-2 border-line-2 pl-3">
            <span className="num text-ink-2">ops_settings</span> is written here and read by the worker every 60 s. The
            boot line <span className="num">alert_strategies_adopted</span> and the refresh line
            {" "}<span className="num">telegram_credentials_applied</span> both name the source they took, so the page
            and the process can be reconciled from the log.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            A document the detectors cannot parse is <em>refused</em>: the worker keeps the last good one and logs{" "}
            <span className="num">alert_strategies_invalid</span>. This page validates with the same ranges first, so the
            two can never disagree about what is armed.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            Every rule writes a <span className="num">signals</span> row whether or not it paged: a cooldown-suppressed
            break is still evidence, and{" "}
            <Link href="/signals" className="link">
              the alert feed
            </Link>{" "}
            shows each row&rsquo;s delivery state next to it.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            A simulated session never pushes on its own — the worker refuses Telegram entirely when it is running
            against sample candles. Send-test is the one exception, because that message is not an alert: it goes when
            you press the button, and it says it is a test.
          </li>
        </ol>
        {!usesPostgres() ? (
          <p className="border-t border-line px-4 py-3 text-[11px] leading-snug text-amber">
            This app is serving the replay sample, so today&rsquo;s counts and the saved documents are not coming from a
            real database. Configure <span className="num">DATABASE_URL</span> to edit the rules the worker obeys.
          </p>
        ) : null}
      </Panel>

      <Panel title="What is not here" hint="and where it is instead">
        <ul className="grid gap-2 px-4 py-3 text-[12px] leading-snug text-ink-2 md:grid-cols-2">
          <li className="border-l-2 border-line-2 pl-3">
            No broker credentials, and no way to place, modify or cancel an order. Angel One keys stay with the worker;
            the dashboard reads a database and writes these two settings rows.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            No qualification thresholds — what makes a stock <em>interesting</em> is on{" "}
            <Link href="/config" className="link">
              Configuration
            </Link>
            , and that one <em>does</em> wait for the next session. These rules are about what is worth a phone call
            right now.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            No retry or queue controls. Telegram delivery attempts, backoff and the permanently-failed state are the
            worker&rsquo;s dispatcher, sized for one sender.
          </li>
          <li className="border-l-2 border-line-2 pl-3">
            No per-stock watch. All three detectors run on the whole configured universe, which is why a break on a
            symbol that never qualified still appears here — the count above each rule is what actually fired today.
          </li>
        </ul>
      </Panel>
    </div>
  );
}
