import Link from "next/link";
import { SimulatedBanner } from "@/components/SimulatedBanner";
import { SubscribeForm } from "@/components/SubscribeForm";
import { ThemeToggle } from "@/components/ThemeToggle";
import { getCurrentUser } from "@/lib/auth/session";
import { isAdminRole } from "@/lib/roles";
import { source } from "@/lib/source";

export const dynamic = "force-dynamic";

/**
 * The public landing page: what the scanner does, what you get for subscribing,
 * what the two plans actually differ on, and how to ask for access.
 *
 * Everything described here exists in the product today — the feature words
 * below map one-to-one onto surfaces a subscriber sees after signing in
 * (Board, Watchlist, News, the Telegram pushes, Account). Nothing on this page
 * requires a session and nothing here reads market data; the only database
 * touch is the subscribe form's own insert into the queue. A signed-in visitor
 * gets the dashboard link in the header because they already have what this
 * page is asking them for.
 */

// The one list a subscriber's day is made of. Kept as data so the tiles and
// the comparison table can never drift apart from each other.
const FEATURES = [
  {
    title: "The daily watchlist",
    body: "One list per trading day: every F&O symbol the scanner qualifies on live 1-minute candles — relative volume, EMA trend, RSI state, breakout structure — each row scored and ranked. It appears here before most people finish their morning coffee.",
  },
  {
    title: "Sector context on every row",
    body: "Each stock wears its NIFTY sector chip, coloured by how that sector index is trending today. A bullish symbol in a bearish sector reads differently, and the page says so without you asking.",
  },
  {
    title: "Sort the list your way",
    body: "Every column header sorts, ascending or descending — score, price, sector, symbol. The watchlist is a working list, not a poster.",
  },
  {
    title: "Alerts that reach your phone",
    body: "Entries, breakouts, RSI state changes, removals and re-qualifications land in the alert feed and page your own Telegram channels — a bot you connect on your Account page, one channel or several.",
  },
  {
    title: "The news desk",
    body: "Free RSS reads on your universe's symbols — what is moving the exact stocks on today's list, next to the list. No paid news API, no black-box sentiment score.",
  },
  {
    title: "A board that admits when it's unsure",
    body: "The Board tracks the live session, the feed's health and the day's counts. When data is stale or a day is simulated, the page says so on its face instead of showing you confident zeros.",
  },
] as const;

// What each plan is *for*, in the buyer's words — not a restatement of the cap.
const PLANS = [
  {
    name: "Basic",
    price: "Top 3 a day",
    pitch: "The proof of the method: the three highest-scoring symbols of every session, decided the way the scanner decides.",
    bullets: [
      "The three best setups of the day, ranked — nothing to wade through",
      "Today's alerts and Telegram delivery for the symbols you see",
      "News desk, sector reads and intraday trend on every listed row",
    ],
  },
  {
    name: "Unlimited",
    price: "The full qualified list",
    pitch: "Everything Basic gets, across the whole universe — the complete day, not its headline.",
    bullets: [
      "Every symbol that clears the gates today — nothing truncated",
      "The complete alert feed and delivery audit for the session",
      "Full membership history: who qualified, who fell out, who came back",
    ],
  },
] as const;

// The honest answer to "what changes when I upgrade" — one row per capability,
// spelled by the two plan columns.
const COMPARE = [
  { row: "Daily watchlist", basic: "Top 3 by score", unlimited: "Every qualified symbol" },
  { row: "Alert feed (today)", basic: "Your symbols", unlimited: "Whole universe" },
  { row: "Telegram delivery", basic: "Yes — your own channels", unlimited: "Yes — your own channels" },
  { row: "News desk & sector reads", basic: "Yes", unlimited: "Yes" },
  { row: "Sortable, scored table", basic: "Yes", unlimited: "Yes" },
  { row: "Membership history", basic: "—", unlimited: "Yes" },
  { row: "Price", basic: "Ask the operator", unlimited: "Ask the operator" },
] as const;

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
          {/* The page's own entry in the row, marked active so the header reads as
              the same navigation bar the dashboard has. */}
          <Link
            href="/subscribe"
            aria-current="page"
            className="rounded-md border border-line-2 px-3 py-1.5 text-[13px] font-medium text-ink"
          >
            Subscribe
          </Link>
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
        {/* ── hero: the promise, then the two doors ─────────────────────────── */}
        <header className="pt-8 pb-10">
          <p className="eyebrow">NSE F&amp;O · minute candles · 09:15–15:30 IST</p>
          <h1 className="font-serif mt-2 text-[40px] italic leading-tight tracking-tight sm:text-[52px]">
            The bullish list, before the market makes it obvious.
          </h1>
          <p className="mt-3 max-w-[640px] text-[14px] leading-relaxed text-ink-2">
            A worker scans the entire F&amp;O universe on every closed minute candle — relative volume, EMA trend, RSI
            state, breakout structure — and writes one watchlist per trading day. Subscribe and that list, its alerts
            and its news become your morning routine. Read-only by design: it never places an order and holds no broker
            key.
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-3">
            <a
              href="#request"
              className="rounded-md border border-line-2 bg-surface-2 px-4 py-2 text-[13px] font-semibold text-accent transition hover:bg-hover"
            >
              Request access
            </a>
            <a href="#plans" className="link text-[13px]">
              Compare the two plans
            </a>
          </div>
          <dl className="mt-8 grid gap-4 border-t border-line pt-5 sm:grid-cols-3">
            <div>
              <dt className="eyebrow">Scan cadence</dt>
              <dd className="mt-1 text-[13px] leading-snug text-ink-2">
                Every closed minute, all day — not an end-of-day report.
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Universe</dt>
              <dd className="mt-1 text-[13px] leading-snug text-ink-2">
                The NSE F&amp;O list, priced under ₹2,000, liquid enough to trade.
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Your side</dt>
              <dd className="mt-1 text-[13px] leading-snug text-ink-2">
                Watch, sort, and get paged. Deciding and trading stay yours.
              </dd>
            </div>
          </dl>
        </header>

        {/* ── features: what a subscriber actually gets ─────────────────────── */}
        <section aria-labelledby="features-h">
          <h2 id="features-h" className="font-serif text-[26px] italic tracking-tight">
            What you get the day you sign in
          </h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            {FEATURES.map((f) => (
              <div key={f.title} className="card px-5 py-5">
                <p className="text-[14px] font-semibold text-ink">{f.title}</p>
                <p className="mt-2 text-[13px] leading-snug text-ink-2">{f.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ── plans ─────────────────────────────────────────────────────────── */}
        <section id="plans" aria-labelledby="plans-h" className="mt-12 scroll-mt-20">
          <h2 id="plans-h" className="font-serif text-[26px] italic tracking-tight">
            Two plans, one method
          </h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            {PLANS.map((p) => (
              <div key={p.name} className="card px-5 py-5">
                <p className="eyebrow">{p.name}</p>
                <p className="mt-1 text-[22px] leading-none text-ink">
                  <span className="num font-semibold">{p.price}</span>
                </p>
                <p className="mt-3 text-[13px] leading-snug text-muted">{p.pitch}</p>
                <ul className="mt-4 space-y-2.5 text-[13px] leading-snug text-ink-2">
                  {p.bullets.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>

          <div className="card mt-4 overflow-x-auto px-5 py-5">
            <table className="w-full min-w-[520px] border-collapse text-left text-[13px]">
              <caption className="pb-3 text-[12px] text-muted">
                The whole difference is breadth. Everything else ships with both plans.
              </caption>
              <thead>
                <tr className="border-b border-line text-[11px] uppercase tracking-[0.14em] text-muted">
                  <th scope="col" className="py-2 pr-3 font-semibold">
                    Capability
                  </th>
                  <th scope="col" className="py-2 pr-3 font-semibold">
                    Basic
                  </th>
                  <th scope="col" className="py-2 font-semibold">
                    Unlimited
                  </th>
                </tr>
              </thead>
              <tbody>
                {COMPARE.map((r) => (
                  <tr key={r.row} className="border-b border-line last:border-0">
                    <th scope="row" className="py-2 pr-3 font-medium text-ink">
                      {r.row}
                    </th>
                    <td className="py-2 pr-3 text-ink-2">{r.basic}</td>
                    <td className="py-2 text-ink-2">{r.unlimited}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* ── the ask + how a day runs ──────────────────────────────────────── */}
        <section id="request" aria-labelledby="request-h" className="mt-12 grid scroll-mt-20 gap-6 lg:grid-cols-[1.1fr_1fr]">
          <div className="card px-5 py-5">
            <p className="eyebrow">Request access</p>
            <h2 id="request-h" className="font-serif mt-1 text-[24px] italic leading-snug tracking-tight">
              Ask, and the operator decides.
            </h2>
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
                  written to the day&apos;s record and pushed to your Telegram channels.
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
