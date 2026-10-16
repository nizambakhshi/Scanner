# The Scanner, explained for a normal person

*A complete list of what this project does, and the exact steps to set it up from an empty
folder to a working system. No jargon without a one-line translation.*

---

## Part 1 — What this thing actually is

Imagine a helper that sits at the stock market all day, watches every company you care
about, and pings your phone the moment one of them starts looking strong — and *nothing
else*. That is this project.

It has three moving pieces:

1. **The worker** — a small Python program you run on your own computer. It connects to
   Angel One (your broker) **only to read the live price stream**, decides which stocks
   look bullish, and writes everything it learns into a database.
2. **The dashboard** — a website (deployed free on Vercel) that *reads* that same
   database and shows you the watchlist, alerts, sectors, news and history. You sign in
   with one email and password.
3. **Telegram** — the worker's mouth. When a stock worth acting on appears, a message
   lands on your phone.

### The one rule that shapes everything

**It never trades.** No button, no alert, no config can place, modify or cancel an order.
Broker passwords exist only so the worker can *listen* to prices. If the website were
hacked tomorrow, the worst an attacker could do is read your watchlist.

---

## Part 2 — The complete feature list

### Watching the market

| Feature | What it means in plain words |
| --- | --- |
| **F&O universe** | Watches the stocks that have futures listed — a curated, liquid slice of NSE (about 200 names), refreshed from the broker's own instrument list. |
| **₹2,000 price ceiling** | Stocks trading above ₹2,000 are not tracked — one tick is too much money for a small account. Adjustable via `MAX_PRICE` (0 turns the ceiling off). |
| **Live 1-minute candles** | The raw tick stream is rebuilt into proper minute bars (open/high/low/close + volume), minute by minute, all session. |
| **Outage handling** | If the broker's feed stalls, the system *freezes* its opinions instead of guessing, marks itself stale on the dashboard, and reconnects on its own. |
| **Catch-up backfill** | A separate helper job fills any minutes the live feed missed, so history stays complete. |

### Deciding what looks strong ("qualified")

A stock joins the watchlist when, on a finished 1-minute candle, all three are true:

1. its price is **above the day's average trading price** (VWAP),
2. its **short-term trend is above its long-term trend** (fast EMA over slow EMA),
3. it is **trading much heavier volume than usual** (relative volume above your threshold).

Once on the list, it gets removed only after 3 consecutive weak candles — the system
won't flip-flop on one red minute. Every stock also carries a **rank** (a weighted score
of price strength, volume, VWAP distance and EMA trend, all four weights editable).

On top of that base rule, the worker computes a full toolbox it uses to *describe*
setups: **RSI** (overbought/entry readings on daily, weekly and intraday 5-minute
timeframes), **MACD**, **ATR** (a volatility ruler), **OBV**, **Donchian channels**,
**ADX**, VWAP-reclaim detection, opening-range breakouts (ORB) and breakout-retests.
There is also an **anti-chase policy**: a checklist that quietly judges whether a move
has already run too far to be worth telling you about.

### Telling you

| Feature | What it means |
| --- | --- |
| **Telegram alerts** | Entries, removals, RSI swings and breakout alerts arrive on your phone, each with a link to the stock's page. |
| **Cooldown** | The same alert for the same stock cannot spam you more than once per 15 minutes (editable). |
| **Alert rules page** | Which strategies push to Telegram is switched on/off from the website, and takes effect within a minute — no restart. |
| **Retry queue** | If Telegram is briefly down, the alert is not lost: it waits in a database queue and retries with back-off. |
| **Saved credentials** | Your Telegram bot token can be stored on the website itself (never displayed again, never logged) — the environment file is only a fallback. |

### The website, page by page

| Page | What you see |
| --- | --- |
| **Board** (`/`) | The day at a glance: market phase, sector tiles (13 Nifty sector indices, green if rising, red if falling), today's headline numbers. |
| **Watchlist** (`/watchlist`) | The heart of the day: a six-stage strip (universe → discovery → watchlist → alerts → qualification → removals), **today's members only** — yesterday's breakouts never leak into today — every stock tagged with its sector in that sector's trend colour, sortable columns (price, change, volume ratio, score…), today's alert feed, and the scanned universe for context. |
| **News** (`/news`) | A live desk of 8 free finance RSS feeds, cleaned and auto-tagged: items about a stock you watch are highlighted, sentiment read as positive/negative. Nothing stored, nothing paid. |
| **Strategy** (`/strategy`) | The ORB board — each stock's first-hour range, whether it broke out or back in, and the retest story, sorted and sector-tagged. |
| **Stock page** (`/instrument/…`) | One stock's day: minute chart sparkline, indicators, alerts it caused, its RSI history. |
| **Alert rules** (`/alerts`) | Switches for each alert strategy + the Telegram token/chat fields + a *Send test* button. |
| **Config** (`/config`) | The scanner's knobs — EMA periods, volume threshold, rank weights, universe — saved to the database and picked up by the worker live. Your saved numbers always outrank the defaults. |
| **Days** (`/history`) | Every past session: member counts, alert counts, coverage. |
| **Health** (`/health`) | Is the worker alive, is the feed streaming, when did data last arrive, how old is the newest candle. |
| **Account** (`/account`) | Change your password. Changing it signs out every other device. |

Small touches: dark **and** light themes that switch instantly without a page reload
(the "Cadence" palette — amber on aubergine at night, plum ink on cream by day), works
down to a 390-pixel phone screen, and a login throttle (8 wrong passwords in 15 minutes
locks that address) with every attempt written to an audit table.

### Housekeeping the system does itself

- **Retention dial** — how many days of minute-data to keep (live site: 3 days; the
  number 0 means "keep everything forever"). The worker prunes old rows automatically.
- **Holiday calendar** — never runs on exchange holidays; knows the 09:15–15:30 session
  window in Indian Standard Time.
- **Daily syncs** — separate one-command jobs refresh yesterday's daily/weekly candles
  and the RSI profile of every stock each evening.

### Development-safety features (why you can trust changes)

- **Replay mode** — the website can run with *no database at all*, showing a complete
  simulated trading day from a sample file, banner-marked, so you can see and test the
  whole UI offline.
- **Simulation mode** — the worker can run on synthetic ticks: no broker, no network,
  real code paths, every row labelled "simulated".
- **763 tests** — every rule above has an automated test; a green CI pipeline on GitHub
  proves the tests pass on every push before the website redeploys.

---

## Part 3 — Setup, from zero to running

The honest order of operations. Steps 1–6 are fifteen minutes if the tools are already
installed. Do step 9 (no-database demo) first if you just want to *see* it work.

### What you need installed

- A Mac or Linux machine (Windows: use WSL). The live worker must stay switched on
  during market hours — any always-on machine works.
- **Node.js 20+** (for the website), **Python 3.12** (for the worker), **git**.
- A free **Supabase** account (that's the database the two halves talk through).
- An **Angel One** account with **SmartAPI** enabled (free; you get an API key, and you
  set a TOTP secret + PIN while activating).
- Optionally, a **Telegram bot** (10 minutes with @BotFather) and a **GitHub** account.

### Step 1 — Get the code

```bash
git clone https://github.com/nizambakhshi/scanner-bullish-scanner.git Scanner
cd Scanner
```

### Step 2 — Create the database

On supabase.com: *New project* → any name, region **ap-south-1 (Mumbai)**, note the
password you set. After it creates: *Project settings → Database → Connection string*
and copy the **URI (connection string)**.

### Step 3 — Create the environment file

```bash
cp .env.example .env
```

Open `.env` and fill in only these for now (leave the rest at their defaults):

| Variable | Where it comes from |
| --- | --- |
| `SCAN_MODE` | `live` — but set `simulation` while learning |
| `DATABASE_URL` | the Supabase connection string from step 2 |
| `AUTH_SESSION_SECRET` | run `openssl rand -hex 32` and paste the result |
| `AUTH_ADMIN_EMAIL` | the email you will sign in with |
| `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`, `ANGEL_REFRESH_TOKEN` | your SmartAPI dashboard |

**`.env` is gitignored — never commit it, never paste it anywhere.**

### Step 4 — Create your login password

```bash
cd app && npm install
npm run hash-password
```

It asks for a password twice, prints two lines like
`AUTH_ADMIN_EMAIL=…` / `AUTH_ADMIN_PASSWORD_HASH=$scrypt$…` — paste those into `.env`.

### Step 5 — Build the tables

Back at the repo root, with the worker's Python set up first:

```bash
cd worker
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
scanner-migrate              # creates all ~40 tables, views and seeds
scanner-sync-instruments     # downloads Angel's instrument list, stores the F&O universe
```

### Step 6 — Run the worker

```bash
scanner-worker
```

In `simulation` mode it immediately shows you a synthetic session in the log — you need
no broker call for this. In `live` mode it logs in, opens the price stream and then
quietly waits for the 09:15 opening. Keep this window (or a background process) running
during market hours.

### Step 7 — Run the website locally

In a second terminal:

```bash
cd Scanner/app
cp ../.env .env.local        # the website reads the same variables
npm run dev                  # → http://localhost:3000
```

Sign in with the email and password from steps 3–4.

### Step 8 — Wire up Telegram

1. Talk to **@BotFather** on Telegram: `/newbot` → copy the **bot token**.
2. Message your new bot once, then get your numeric chat id from **@userinfobot**.
3. Either put `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` into `.env`, or — better — open
   the **Alert rules** page on the website and save them there: they take effect without
   a restart and the page has a **Send test** button.
4. Open the **Config** page, keep your eye on the dashboard; on the first live session
   alerts start arriving.

### Step 9 — The zero-infrastructure demo (do this any time)

Want to see the whole website without a database, broker or Telegram?

```bash
cd Scanner/worker && source .venv/bin/activate
scanner-simulate               # replays 13 scripted market stories end-to-end
scanner-dashboard-data         # saves that fake day as the website's sample data

cd ../app
DATABASE_URL= AUTH_SESSION_SECRET=$(openssl rand -hex 32) npm run build
DATABASE_URL= npm run start    # every page renders in "simulated replay" mode
```

### Step 10 — Put it online

1. Push the repo to GitHub (you already have it locally; `git push` your own fork).
2. On vercel.com: *Import* that repo. It auto-detects Next.js, but the app lives in the
   `app/` folder — set **Root Directory = `app`**.
3. Vercel → Settings → Environment Variables: paste `DATABASE_URL`,
   `AUTH_SESSION_SECRET`, `AUTH_ADMIN_EMAIL`, `AUTH_ADMIN_PASSWORD_HASH`,
   `AUTH_SESSION_HOURS`. **Do not give the website any Angel or Telegram env values** —
   tokens saved on the Alert rules page work, and a read-only website needs nothing
   else.
4. Deploy. Every future `git push` to `main` redeploys automatically, *after* the test
   pipeline goes green.
5. Supabase → Settings → Database: turn **off** connection pooling the first time if the
   worker complains about `sslmode`; the shipped connection string uses `sslmode=require`
   for the website and the worker accepts both spellings.

### Step 11 — Daily rhythm once it's yours

| When | Command / action | Why |
| --- | --- | --- |
| Every evening | `scanner-sync-daily && scanner-sync-profiles` | tomorrow's daily/weekly candles and RSI readings |
| Anytime | Health page | worker heartbeat, feed state, data age at a glance |
| Anytime | Config page | tune thresholds; the worker picks changes up within 60 seconds |
| Market days 09:15 | nothing | the worker starts itself; alerts flow automatically |

### Step 12 — Check your work like the maintainers do

```bash
cd app    && npm run typecheck && npm run lint && npm test
cd worker && ruff check worker tests && pytest tests/unit tests/e2e
```

All green = your copy matches what CI tests on every push.

---

## Troubleshooting a layman will actually hit

| Symptom | The real reason, usually |
| --- | --- |
| Login rejects the correct password | You changed `.env` after bootstrapping — once `app_users` has your email, the hash row wins. Use `/account` or `npm run reset-password`. |
| Worker exits with "missing keys" | It prints exactly which `.env` names are unset — set those, not others. |
| No Telegram alerts, website fine | The Alert rules page shows the Telegram fingerprint; if it says unconfigured, save the token/chat there. |
| Dashboard says "feed stale" | The worker isn't running, or Angel closed the session — check the worker terminal, restart it. |
| Website shows "replay mode" banner | It can't see your database — `DATABASE_URL` missing or wrong in the web environment. |
| Empty watchlist at 09:20 | Normal — the first qualifications need a few complete minutes; the universe scan completes after ~5 candles. |

*Prices, rules and thresholds in this guide are the shipped defaults; your Config page
is the authority on what your copy actually uses.*
