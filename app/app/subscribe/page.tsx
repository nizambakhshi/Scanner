import Link from "next/link";
import { SimulatedBanner } from "@/components/SimulatedBanner";
import { SubscribeForm } from "@/components/SubscribeForm";
import { ThemeToggle } from "@/components/ThemeToggle";
import { getCurrentUser } from "@/lib/auth/session";
import { isAdminRole } from "@/lib/roles";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * The public side of the product: what the scanner is, what the two plans
 * mean, and how to ask for access. Nothing on this page requires a session and
 * nothing here reads market data — the only database touch is the subscribe
 * form's own insert into the queue. A signed-in visitor gets the dashboard link
 * in the header because they already have what this page is asking them for.
 */
export default async function SubscribePage() {
  const [user, meta] = await Promise.all([getCurrentUser(), source().meta()]);

  return (
    <div className="min-h-screen">
      <div className="mx-auto flex max-w-[1400px] items-center justify-between px-4 py-3">
        <Link href={user ? "/" : "/login"} className="flex items-baseline gap-2">
          <span className="font-serif text-[17px] italic leading-none tracking-tight">Intraday Scanner</span>
          <span className="font-display hidden text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-2 sm:inline">
            NSE · 1m
          </span>
        </Link>
        <div className="flex items-center gap-2">
          {user ? (
            <Link
              href={isAdminRole(user.role) ? "/" : "/watchlist"}
              className="rounded-md border border-line px-3 py-1.5 text-[13px] font-medium text-ink-2 transition hover:bg-hover hover:text-ink"
            >
              Open dashboard
            </Link>
          ) : (
            <Link
              href="/login"
              className="rounded-md border border-line px-3 py-1.5 text-[13px] font-medium text-ink-2 transition hover:bg-hover hover:text-ink"
            >
              Sign in
            </Link>
          )}
          <ThemeToggle />
        </div>
      </div>
      <SimulatedBanner meta={meta} />

      <main className="mx-auto max-w-[980px] px-4 pb-16">
        <header className="pt-8 pb-10">
          <p className="eyebrow">NSE F&amp;O · minute candles · 09:15–15:30 IST</p>
          <h1 className="font-serif mt-2 text-[40px] italic leading-tight tracking-tight sm:text-[52px]">
            The bullish list, before the market makes it obvious.
          </h1>
          <p className="mt-3 max-w-[640px] text-[14px] leading-relaxed text-ink-2">
            A worker scans the F&amp;O universe on every closed minute candle — relative volume, EMA trend, RSI state,
            breakout structure — and writes one watchlist per trading day. This dashboard shows that list, the alerts
            it raised, and the news moving the same symbols. It is read-only: it never places, modifies or cancels an
            order, and it holds no broker key.
          </p>
        </header>

        <section className="grid gap-4 sm:grid-cols-2">
          <div className="card px-5 py-5">
            <p className="eyebrow">Basic</p>
            <p className="mt-1 text-[22px] leading-none text-ink">
              Top <span className="num font-semibold">3</span> a day
            </p>
            <ul className="mt-4 space-y-2.5 text-[13px] leading-snug text-ink-2">
              <li>The three highest-scoring symbols of each session, ranked the way the scanner ranks them.</li>
              <li>Today&apos;s alerts for the symbols you see, with Telegram delivery on your own channels.</li>
              <li>The news desk, the sector reads, and the intraday trend on every listed row.</li>
            </ul>
          </div>
          <div className="card px-5 py-5">
            <p className="eyebrow">Unlimited</p>
            <p className="mt-1 text-[22px] leading-none text-ink">
              The <span className="num font-semibold">full</span> qualified list
            </p>
            <ul className="mt-4 space-y-2.5 text-[13px] leading-snug text-ink-2">
              <li>Every symbol that clears the gates today — nothing truncated, all membership history.</li>
              <li>The complete alert feed and delivery audit for the session.</li>
              <li>Everything Basic includes, across the whole universe the scanner scans.</li>
            </ul>
          </div>
        </section>

        <section className="mt-10 grid gap-6 lg:grid-cols-[1.1fr_1fr]">
          <div className="card px-5 py-5">
            <p className="eyebrow">Request access</p>
            <h2 className="font-serif mt-1 text-[24px] italic leading-snug tracking-tight">Ask, and the operator decides.</h2>
            <p className="mt-2 text-[13px] leading-snug text-muted">
              There is no checkout — this product takes no card and sends no mail. Your request lands on the
              operator&apos;s Accounts page; approval creates the account with the plan you asked for, and the first
              password is handed to you personally. Sign in after that, and change the password on your Account page.
            </p>
            <div className="mt-4">
              <SubscribeForm />
            </div>
          </div>
          <div className="space-y-4">
            <div className="card px-5 py-5">
              <p className="eyebrow">How a day runs</p>
              <ol className="mt-3 space-y-2.5 text-[13px] leading-snug text-ink-2">
                <li>
                  <span className="num font-semibold">09:15</span> — the session opens; the scanner reads the live
                  candles and starts qualifying symbols.
                </li>
                <li>
                  <span className="num font-semibold">All day</span> — every membership change, alert and removal is
                  written to the day&apos;s record and pushed to the operator&apos;s Telegram channels.
                </li>
                <li>
                  <span className="num font-semibold">15:30</span> — the session closes; the list freezes and the day
                  moves to the history page.
                </li>
              </ol>
            </div>
            <div className="card px-5 py-5">
              <p className="eyebrow">Honest limits</p>
              <p className="mt-3 text-[13px] leading-snug text-ink-2">
                Holidays follow the operator&apos;s calendar, not an exchange feed — a day can be marked off without
                notice. Signals are observations of price and volume, not advice; nothing here trades for you, and a
                symbol on the list is not a promise it keeps going.
              </p>
            </div>
          </div>
        </section>

        <footer className="mt-12 border-t border-line pt-4 text-[11px] leading-snug text-muted">
          <p>
            Basic shows three symbols because three good ones are worth more than thirty noise. Upgrade by asking the
            operator — the same form works for that.
          </p>
        </footer>
      </main>
    </div>
  );
}
