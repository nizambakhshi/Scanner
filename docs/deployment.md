# Deployment

Two separate deployments, deliberately. The **worker** runs where the broker credentials
are; the **dashboard** runs where they are not. Nothing about this split is cosmetic: the
dashboard's configuration module (`app/lib/env.ts`) reads an allowlist of variable names,
and a name suggesting a broker or messaging key does not belong on it.

---

## 1. GitHub (private)

The repository should be **private**. It contains no secrets, but it does contain the
mapping rules, universe handling and alert behaviour of a live trading-adjacent system,
and the `.env.example` names every variable that *would* hold one.

```bash
cd ~/Desktop/Scanner
git init                          # already done here
git add -A
git commit -m "…"
gh repo create scanner-bullish-scanner --private --source=. --push
```

Before the first push, confirm nothing secret is staged:

```bash
git status --short                 # no .env, no app/.next, no node_modules
git ls-files | grep -i "env"       # only .env.example
```

`.gitignore` at the root and `app/.gitignore` exclude `.env*`, `node_modules/`,
`app/.next/` and `.vercel/`. If a real environment file is ever committed by mistake,
rewriting history is not enough — rotate the credentials.

---

## 2. The worker

Anywhere that keeps a process alive and can hold the broker keys: a small VM, a Fly.io
machine, a Raspberry Pi on the office network. It needs:

- Python 3.12, `pip install -e .` from `worker/`
- network access to `apiconnect.angelone.in` and the SmartAPI WebSocket host
- the Supabase database URL
- a process supervisor (`systemd`, `launchd`, or `fly machines`) — the worker is long-lived
  and must restart after a crash, because a scanner that died quietly looks identical to a
  quiet market

```bash
cd worker
cp ../.env.example .env    # fill in ANGEL_* and TELEGRAM_* yourself
scanner-migrate
scanner-sync-instruments
scanner-worker
```

The Angel One credentials are the API key, client code, TOTP secret, PIN and a refresh
token. The refresh token is minted once through the broker portal and rotated by the
worker; it never leaves that process. The Telegram bot token and chat id reach the worker
by either route: `TELEGRAM_*` in this process's environment, or the pair an operator saves
on the dashboard's Alerts page, which lands in `ops_settings['telegram']` and is re-read
every 60 seconds and applied inside the running session. A complete stored pair wins over
the environment; an empty or half-filled row does not, and a stored pair switched off on
the page is blanked rather than left configured. Only the worker dispatches real alerts.

---

## 3. The dashboard on Vercel

Import the private GitHub repository. **Root Directory: `app`** — the Next.js app lives in
that subdirectory, and a build from the repository root will fail.

Framework: Next.js. No build command overrides are needed (`npm run build`), and no output
directory (server-rendered, not static).

### Environment variables

Set these in Vercel → Project → Settings → Environment Variables. The dashboard can do
nothing without them, and it will say which names are missing on its health page rather
than guess.

| Variable | Required | What it does |
| --- | --- | --- |
| `DATABASE_URL` | yes, for live data | Direct PostgreSQL connection to Supabase |
| `AUTH_SESSION_SECRET` | yes | 32+ characters; signs the session cookie |
| `AUTH_ADMIN_EMAIL` | first sign-in | The operator address bootstrapped into `app_users` |
| `AUTH_ADMIN_PASSWORD_HASH` | first sign-in | `scrypt$<salt>$<hash>` from `npm run hash-password` |
| `DATABASE_SSL` | usually leave unset | `require` by default; `disable` is for a local plaintext server and is reported on the health page |
| `DATA_SOURCE` | no | `auto` normally; `replay` forces the offline sample for a review build |
| `AUTH_SESSION_HOURS` | no | Session lifetime, default 12 |
| `APP_URL` | no | Canonical URL used in cookie scope |

Generate the password hash locally and paste the printed lines into Vercel:

```bash
cd app && npm run hash-password
```

The hash contains `$` characters. In a shell that means it must be single-quoted, and
sourcing a file that double-quotes it silently expands them into a corrupted value:

```bash
# correct
export AUTH_ADMIN_PASSWORD_HASH='scrypt$1f2e…$9c7a…'
# wrong — the shell eats $1f2e… and $9c7a… as variables
export AUTH_ADMIN_PASSWORD_HASH="scrypt$1f2e…$9c7a…"
```

Vercel stores values literally, so this only bites when running the dashboard from a
terminal or a `.env`-sourcing script. There is a second, quieter version of the same trap:
**`next dev`/`next start` runs every variable that also appears in a `.env*` file through
dotenv-expand, including one supplied on the command line**, and an un-braced `$1a2b`
there is treated as a variable reference and replaced with nothing. Proven locally
2026-10-15: `AUTH_ADMIN_PASSWORD_HASH='scrypt$…$…' next dev` in a directory whose
`.env.local` names that same key made the server bootstrap the admin row with the literal
string `scrypt`, so every sign-in 401'd while the env file itself read correctly. It cannot
happen on Vercel (no `.env*` files there, so nothing is expanded), but locally the fix is to
put the value in the env file rather than the command line, or escape the dollars as `\$`.

**No `ANGEL_*`, TOTP or PIN variable belongs on the Vercel project**, and the app has no
code path that reads them; adding them would put broker credentials in a build log's reach
for no capability gained.

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are the one deliberate exception, added on
2026-10-14. They are *optional* on the dashboard: `app/lib/env.ts` reads them only as the
fallback behind the pair stored in `ops_settings['telegram']`, so the **Send test** button
on the Alerts page can prove a credential that was never typed into the page. Setting them
is not required — leaving them unset is the more conservative choice, and the page then
says the credential lives "nowhere yet" until you save one. A token is never returned by
any view, rendered into HTML, written to an audit row or logged: only its 12-character
sha256 fingerprint is shown, which is the same fingerprint the worker logs when it adopts
a pair. Nothing on the dashboard can place, modify or cancel an order.

### Network

Supabase's direct connection (`db.<ref>.supabase.co:5432`) is what `DATABASE_URL` should
point at. If the project sits behind a restrictive network, use the session pooler on port
5432 — not the transaction pooler on 6543, which breaks prepared statements. The health
page detects a `:6543` URL and reports that statements are being inlined instead of
assuming the connection is healthy.

### After the first deploy

1. Open `https://<project>.vercel.app/login` and sign in.
2. Go to **Health**. It must say `PostgreSQL`, `connected`, and list no missing variables.
   If it says `Replay sample`, `DATABASE_URL` did not reach the deployment.
3. Confirm the whole screen still shows the simulated banner when the sample is being
   served, and shows no banner when the database is.
4. Sign out and reload a page: it must return to `/login`, and `/api/watchlist` must be
   `401`. So must `/api/strategies`, `/api/telegram` and `/api/telegram/test` — the last
   one is the only endpoint in the app that talks to an outside service, and it never runs
   without a signed-in session.
5. Open **Alert rules**. It renders the three level detectors with the numbers the worker
   ships, one Telegram panel, and a note about what a save does. Changing a threshold and
   saving must leave the page reading back the new value, and must put exactly one
   `ops_settings['alert_strategies']` row and one `audit_logs` row with the field-level
   diff in the database. The worker picks it up within a minute — its log says
   `alert_strategies_adopted`, never a copy of the token.

Vercel's deployment protection (or any auth in front of the app) is optional; the app
authenticates itself. Do not disable it and expose the API to the open internet with a
weak session secret — `AUTH_SESSION_SECRET` is the only thing between a request and every
number on these pages.

---

## 4. What has and has not been verified

Verified locally against a production build: the offline replay session end to end through
the real scanner pipeline; all 46 dashboard tests; type-check, lint and build; every page
and API route rendered against a running server, including login, the eight-failure
lockout, sign-out revocation and unauthenticated redirects.

Not verified, because it needs credentials and a live market: the Postgres source and its
views, the migrations against a real Supabase instance, Telegram delivery, and the
SmartAPI WebSocket. The first live run should be watched against the health page, and the
first session compared against the broker's own terminal for a couple of symbols.
