# Staging tunnel — the public URL for the work-in-progress board

`https://scanner-staging.maktaba.co.in` is a Cloudflare Tunnel that reaches the staging
dashboard on this Mac Mini (`127.0.0.1:3100`) without opening anything on the router and
without the Mini needing a public IP. The connection is **outbound** from the Mini to
Cloudflare; nothing listens for inbound traffic.

The live board is not here. It stays on Vercel reading Supabase Mumbai. This path only
ever points at the staging copy, and `staging-tunnel.sh` refuses to start if the ingress
file names a database port.

## The files

| | |
| --- | --- |
| `~/.cloudflared/config.yml` | the ingress: one hostname → `http://127.0.0.1:3100`, then the mandatory `http_status:404` catch-all. A hostname that is not in this file cannot be served. |
| `com.scanner.staging-tunnel.plist` | the launchd job, installed to `~/Library/LaunchAgents/` by `staging-tunnel.sh install`. KeepAlive, so a dropped tunnel reconnects. |
| `staging-tunnel.sh` | `install` · `open` · `close` · `status` · `preflight` |
| `check_http.sh` | asks ~50 machines around the world (and one in India) what this URL looks like to them |
| `set-staging-password.sh` | gives staging its own sign-in credential, held in the Keychain |

## Opening and closing

`open` runs a preflight and **refuses** unless:

1. the ingress file is sane (exactly one hostname, a 404 catch-all, no database port),
2. something answers on `127.0.0.1:3100`,
3. `scanner-staging-admin` exists in the Keychain — i.e. staging is *not* signing people
   in with the production password, and
4. `~/.config/scanner-staging/gate.confirmed` exists, which is a dated statement that
   the Cloudflare filter below was created.

`--force` overrides 4 and shouts while doing it. `close` always works, needs no
confirmation, and proves itself by re-probing the hostname (530 = Cloudflare has no
tunnel to route to).

**Current state: open** (since 2026-10-14 evening), with the country rule live and
verified from both vantages below. `status` prints the launchd state, what the origin
answers locally, and what the public hostname answers from this machine.

## Why the password had to be split first

The Mini's database is a copy of the Mumbai project, and the copy included `app_users`.
`bootstrapAdmin()` only inserts from the environment while that table is **empty**, so
the staging board was authenticating against the row that arrived from production —
the production password opened both boards. `set-staging-password.sh` replaces the hash
in the Mini's row with one derived from a generated 24-character secret stored in the
login Keychain, keeps `AUTH_ADMIN_PASSWORD_HASH` in the staging env consistent for the
day the table is rebuilt, and proves the change by signing in over HTTP:

```
security find-generic-password -s scanner-staging-admin -w | pbcopy
```

## The network filter (no Cloudflare Access, no card)

Cloudflare Access would be the right answer, but Zero Trust Free still requires a card
on file to activate, so staging is filtered with a zone **custom rule** instead — the
free plan allows 5, and this uses one. Dashboard path:
*maktaba.co.in → Security → Security rules → Custom rules → Create rule*.

```
(http.host eq "scanner-staging.maktaba.co.in" and ip.geoip.country ne "IN")
```

Action **Block**. It is scoped by hostname on purpose: an unscoped country rule would
filter the whole domain, not just the staging board.

What this does and does not do: it removes the entire non-Indian internet from the
equation, which is where the scanning and credential-stuffing noise comes from. It does
**not** authenticate anyone — Indian visitors still meet the staging password, and the
dashboard's own throttle (8 failed attempts per 15 minutes per email + IP) still applies.
Anyone working from outside India needs the rule widened or must sign in from home.

## The tunnel and `next dev` do not agree

`next dev` refuses to serve its own JavaScript to a Host it doesn't recognise, and
`scanner-staging.maktaba.co.in` is not one it knows. It answers those requests with a
bare `401 Unauthorized` — which is invisible from the outside: the server-rendered HTML
arrives fine, so the login page **looks** healthy, but React never hydrates, the form's
`onSubmit` never attaches, and pressing Sign in makes the browser do a **native GET
submit** that puts the typed credentials in the URL, the browser history and the dev log.
No `POST /api/session` is ever sent, so nothing is recorded in `login_attempts` either —
the failure looks like a wrong password when it is a missing script.

The fix is Next's own: name the origin in `allowedDevOrigins`, fed by
`DEV_ALLOWED_ORIGINS` in the staging `.env.local` so the hostname never travels into the
deployed copy's config. `check_http.sh` will not catch this — curl never needs hydration.

Two consequences worth stating plainly. A `curl` 200 on the tunnel proves the *tunnel*,
not the *page*: everything in the verification list below was done with curl, and the
browser was still broken. And if the board ever shows credentials in its address bar,
clear that history entry and scrub the log — `: > /tmp/staging-app.log`.

## Verification actually performed (2026-10-14)

- Tunnel + DNS route created with `cloudflared` (tunnel `scanner-staging`,
  `15a097df-a167-40f0-b685-9ce75cc887b9`); while running it served
  `https://scanner-staging.maktaba.co.in/login` → HTTP 200 over four QUIC connections.
- With the tunnel stopped, the same URL returns **HTTP 530** — verified, so "closed"
  means closed rather than "probably not listening".
- Staging sign-in: wrong password → 401, Keychain password → 200 with a session cookie,
  and `app_users.last_login_at` moved.
- The staging dashboard now binds `127.0.0.1:3100` (`dev:staging`/`start:staging` pass
  `-H 127.0.0.1`). Before that it answered on `*:3100`, i.e. every machine on the LAN.
- **The country rule, proved while the door was still shut.** A request the WAF blocks
  never reaches the tunnel, so with `cloudflared` stopped the two answers separate
  cleanly: `check_http.sh` reported **403 on 46 of 46 answered foreign nodes** and
  **530 on its one Indian node** — blocked abroad, merely un-served at home. That is the
  only proof available before opening, and it shows the rule discriminates by country
  rather than locking the operator out.
- **After `open`**: the same probe read **200 from India, 403 from all 46 abroad nodes**,
  with `cloudflared` under launchd (pid 58498, KeepAlive, never exited).
- **A real sign-in through the proxy**: `POST /api/session` → 200, `Set-Cookie:
  scanner_session=…; HttpOnly; SameSite=lax`, and that cookie then returned **200 on the
  board and on `/api/health`** — cookies survive Cloudflare. Without it the board is
  307→`/login` and `/api/health` is 401, so the tunnel did not weaken the app's own wall.
- One gap worth naming: the staging cookie carries **no `Secure` flag**, because
  `lib/auth/session.ts:106` sets `secure: env.nodeEnv === "production"` and staging runs
  under `next dev`. Production on Vercel has `NODE_ENV=production`, so its cookie is
  `Secure`; this board's is not. Cloudflare only serves this hostname over HTTPS, so
  nothing is exposed in practice — but if staging ever runs `start:staging` the flag
  appears, and until then the honest reading is that the tunnel's protection is the
  country rule plus the password, not transport security.
- **The browser path, after the `allowedDevOrigins` fix**: the tunnelled login page loads
  its Turbopack dev chunks (200 with the tunnel's `Origin`, where they were 401 before),
  the log records no cross-origin block, and clicking the theme control flips
  `DARK`→`LIGHT` without a navigation — which is the observable proof that React hydrated
  and that Sign in will now `POST` instead of submitting the form as a GET query.

## Related

- `ops/db-edge/` — the *database* edge for Vercel→Mini, a different problem with a
  different answer (PgBouncer + a router forward, parked).
- `ops/staging-db/fix-sequences.sh` — the clone defect that made staging sign-in return
  HTTP 500; read it before trusting any copied database.
