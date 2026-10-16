# Intraday Scanner

A market-data scanner for NSE equities. It watches the Nifty 500 universe during live
sessions, keeps a daily watchlist of symbols that are trending bullishly, and tells an
operator about entries, weakenings and removals on Telegram and on a web dashboard.

**It never trades.** Nothing in this repository places, modifies or cancels an order.
Broker credentials are used for one purpose: reading the live market-data stream. The
dashboard is a reader of a database the scanner writes to; it holds no broker
credentials, and its configuration module refuses to read variables whose names suggest
they do.

Telegram is the one exception, added deliberately on 2026-10-14 at the operator's
request: the [Alert rules](app/app/(shell)/alerts/page.tsx) page stores a bot token and
chat id so a rule can be pointed at a phone from the browser instead of only from the
worker's environment, and its *Send test* button is the only message the dashboard ever
sends. That pair is write-only — the page never shows it back, and it appears in no HTML,
log line, API response or audit row, only as a 12-character sha256 fingerprint. Real
alerts are still raised, deduped, rate-limited and delivered by the worker.

## The two processes

| | Scanner worker (`worker/`) | Dashboard (`app/`) |
| --- | --- | --- |
| Runs | on a machine that can hold broker keys | anywhere, including a hosting platform |
| Does | subscribes to the SmartAPI WebSocket, builds 1-minute candles, computes VWAP/EMA/relative volume, qualifies symbols, maintains the watchlist state machine, raises alerts, delivers to Telegram | reads the database and renders the watchlist, alert feed, session history, configuration and health |
| Writes | PostgreSQL (Supabase) | two `ops_settings` rows — `alert_strategies` and `telegram` — plus their audit rows |
| Secrets it can read | `ANGEL_*`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `DATABASE_URL`, `AUTH_SESSION_SECRET`, admin email + password hash, and `TELEGRAM_*` only as the fallback when nothing is saved on the page |

They communicate through PostgreSQL. A dashboard that is compromised, cached or
screenshotted cannot place a trade — it cannot reach the broker at all — and the one
outbound message it can produce is the test a signed-in operator asked for, which says in
its own text that it is a test.

## Two environments

The live app and the work-in-progress app are two copies of this repository on two
databases, so a half-finished screen is never what an operator is looking at during a
session.

| | Live | Staging |
| --- | --- | --- |
| Directory | `~/Desktop/Scanner` | `~/Desktop/Scanner-staging` |
| Git | branch `main`, pushed to GitHub | branch `staging`, kept local |
| Dashboard | Vercel, plus `localhost:3000` as a mirror of it — both read **Supabase Mumbai** | `127.0.0.1:3100` — reads the **Mac Mini's own Postgres** on 54322 |
| Worker | `worker/`, `WORKER_ID=worker-local` | `Scanner-staging/worker/`, `WORKER_ID=worker-staging` |
| Telegram | delivered from this copy | `TELEGRAM_*` blank on purpose: this copy must not send a second copy of an alert. A pair saved on the Alerts page lives in *that* environment's `ops_settings`, so staging's button uses the Mini's row and never the live one |

The env files are gitignored, so each copy carries its own credentials and its own
`DATABASE_URL`; everything about the split that *is* in the code is two npm scripts and
one diagnostic. A feature is written in `Scanner-staging`, checked against the Mini, then
`git checkout main && git merge staging` ships it and Vercel redeploys `main` on Mumbai.

Two rules make the arrangement hold:

- **Do not push `staging`.** A push makes Vercel build a preview deployment, and a
  preview is handed production's `DATABASE_URL` — an untested screen would be reading,
  and through the admin routes writing, the live database.
- **Open staging at `http://127.0.0.1:3100`, never `http://localhost:3100`.** The session
  cookie is keyed by host name, so two spellings of the same machine let the two copies
  trade cookies and log each other out. Their `AUTH_SESSION_SECRET` values differ, so the
  symptom is a login page that keeps rejecting a correct password.

The staging worker holds a **second, simultaneous SmartAPI session on the same client
code**, which is what it takes to feed the Mini with live data. If Angel One ever
invalidates the live worker's login when a second one appears, set `SCAN_MODE=simulation`
in `Scanner-staging/worker/.env`: it replays synthetic ticks, touches no broker endpoint,
and keeps the Mini's tables full for UI work.

To answer "which database is this copy really talking to" — worth asking before any test
that writes — run `npm run which-db` inside a copy's `app/`. It prints the masked
connection string, the TLS mode, and `inet_server_addr()` as reported by the server
itself, which is the one field that cannot lie about which cluster answered.

Staging can also be reached from outside the house, through a Cloudflare Tunnel that
points at `127.0.0.1:3100` and nothing else: `ops/staging-tunnel/staging-tunnel.sh
open|close|status`, serving `https://scanner-staging.maktaba.co.in`. It is **open since
2026-10-14**, behind a Cloudflare country filter and a staging-only Keychain password;
`open` refuses to start unless both are in place (`set-staging-password.sh` and a
confirmed zone rule), and `close` needs no confirmation. See
[`ops/staging-tunnel/README.md`](ops/staging-tunnel/README.md) for what that gate does
and, importantly, what it does not.

## Signing in, and changing the password

Sign-in is one email and one password against `app_users` (a scrypt hash; the first login
bootstraps the admin from `AUTH_ADMIN_EMAIL`/`AUTH_ADMIN_PASSWORD_HASH` **only while that
table is empty** — a cloned database therefore keeps the source's password, not the new
environment's). Eight failures from one address in fifteen minutes lock that address out,
and every attempt, successful or not, is written to `login_attempts`.

**Account** (`/account`) changes the password: it asks for the current one, applies the
same strength rules as sign-in, and revokes every other session so an unrecognised login
is ended by the same action that replaced the secret. Wrong current passwords are
throttled exactly like a failed sign-in.

There is no emailed reset link, and that is deliberate: this app sends no mail, and an
endpoint that hands out a usable token to whoever names an address is a door rather than a
recovery path. If the password itself is lost, prove ownership with the database
connection instead — on the machine that holds it:

```bash
cd app && npm run reset-password -- --email you@example.com
```

It prompts twice with the input hidden, takes the password from no argument, confirms the
target database by name before writing (two copies of this app exist on one machine, and a
reset aimed at the wrong one is silent), refuses anything `passwordProblem()` would
refuse, hashes through the app's own `lib/auth/password.ts`, and revokes all sessions.

## What "qualified" means

A symbol joins the watchlist when, on a **completed** 1-minute candle:

1. close is above the session VWAP,
2. EMA9 is above EMA20,
3. relative volume (this candle against the mean of the previous 20) is at or above the
   configured multiple (1.5 by default).

Ranking is assigned once, by the initial scan after the first candles of the session,
from weighted price strength, relative volume, VWAP distance and EMA spread. Symbols
that qualify later join the list unranked rather than receiving a mid-session rank that
would not be comparable. A membership is removed after three consecutive weak candles,
and an alert per symbol per type is rate-limited by a 15-minute cooldown.

When the feed goes quiet for longer than the staleness threshold, memberships **freeze**:
no state change and no removal can be decided on absent data, and the dashboard says so.

## Layout

```
worker/              Python 3.12 package: ingestion, indicators, watchlist, alerts, CLI
  worker/marketdata/ SmartAPI auth + REST + WebSocket feed, instrument master, candle builder
  worker/analytics/  indicators (VWAP, EMA, relative volume), qualification, ranking
  worker/persistence/ Postgres store, in-memory store used by replay, shared protocol
  worker/simulation/ scripted scenarios and the offline replay runner
  worker/scripts/    migrate, sync-instruments, dashboard-fixture export
  worker/            pipeline, watchlist state machine, alerts, Telegram, dispatcher
  tests/             unit, e2e (offline) and integration (needs a test database)
app/                 Next.js 16 dashboard: pages + a JSON API that reads the tape and
                     writes only the operator's own settings (config, alert rules,
                     Telegram pair, password, retention)
supabase/migrations/ 0001–0010: tables, roles and security, read views, seed config,
                     daily/weekly candles, alert strategies and Telegram settings
data/                checked-in Nifty 500 universe and NSE holiday calendar
docs/                research notes and the deployment guide
```

## Running the dashboard without a database

The dashboard has two data sources behind one interface. With no `DATABASE_URL` it serves
`app/data/replay-fixture.json` — a session produced by the offline simulator, with
synthetic symbols (`SIMREENTRY`, `SIMBREAK`, `SIMFADE`, …), every row labelled
`is_simulated`, and a persistent banner on every page. **When a database is configured the
app never falls back to that sample**: a failed query renders as a failed panel, because a
screen of synthetic candles that looks like a market is the failure this design refuses.

```bash
cd app
npm install
npm run hash-password      # prints AUTH_ADMIN_* lines; paste them into your environment
npm run dev                # http://localhost:3000
```

## Running a simulated session (no broker, no network)

```bash
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -e .
scanner-simulate                 # replays every scripted scenario through the real pipeline
scanner-simulate --list-scenarios
scanner-dashboard-data           # exports that session to app/data/replay-fixture.json
```

## Running against the live market

```bash
cd worker
cp ../.env.example .env      # fill in the ANGEL_* and TELEGRAM_* values yourself
scanner-migrate              # applies supabase/migrations in order
scanner-sync-instruments     # downloads the instrument master, maps the universe
scanner-worker               # connects to SmartAPI and scans the session
```

`scanner-worker` needs `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_TOTP_SECRET`,
`ANGEL_PIN` and a `ANGEL_REFRESH_TOKEN`. Those belong to the worker only. The dashboard
has no code path that reads them, and the SQL the dashboard issues lists columns
explicitly, so `instrument_token` and delivery destinations cannot arrive in a response.

## Checks

```bash
cd app    && npm run typecheck && npm run lint && npm test && npm run build
cd worker && ruff check worker tests && pytest tests/unit tests/e2e
```

The 12 Postgres integration tests are skipped unless `TEST_DATABASE_URL` points at a
disposable database (`pytest tests/integration`).

## Current state, honestly

Verified: the offline simulated session end to end through the real pipeline; the
dashboard's 46 unit tests; type-check, lint and production build; every page and API route
rendered and guarded against a running production server (login, throttling after eight
failed attempts, sign-out revocation, 401 on the API without a session).

Not yet verified: any code path that requires a real database or a real broker login —
the Postgres source, the views, migrations, Telegram delivery, and the SmartAPI stream.
Those need credentials and a live session, and the first run should be watched against
the health page.
