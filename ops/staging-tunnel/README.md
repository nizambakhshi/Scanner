# Staging tunnel — the public URL for the work-in-progress board

`https://scanner-staging.maktaba.co.in` is a Cloudflare Tunnel that reaches the staging
dashboard on this Mac Mini (`127.0.0.1:3100`) without opening anything on the router and
without the Mini needing a public IP. The connection is **outbound** from the Mini to
Cloudflare; nothing listens for inbound traffic.

The live board is not here. It stays on Vercel reading Supabase Mumbai. This path only
ever points at the staging copy, and `staging-tunnel.sh` refuses to start if the ingress
file names a database port.

## The three files

| | |
| --- | --- |
| `~/.cloudflared/config.yml` | the ingress: one hostname → `http://127.0.0.1:3100`, then the mandatory `http_status:404` catch-all. A hostname that is not in this file cannot be served. |
| `com.scanner.staging-tunnel.plist` | the launchd job, installed to `~/Library/LaunchAgents/` by `staging-tunnel.sh install`. KeepAlive, so a dropped tunnel reconnects. |
| `staging-tunnel.sh` | `install` · `open` · `close` · `status` · `preflight` |
| `set-staging-password.sh` | gives staging its own sign-in credential, held in the Keychain |

## Default state: closed

`staging-tunnel.sh open` runs a preflight and **refuses** unless:

1. the ingress file is sane (exactly one hostname, a 404 catch-all, no database port),
2. something answers on `127.0.0.1:3100`,
3. `scanner-staging-admin` exists in the Keychain — i.e. staging is *not* signing people
   in with the production password, and
4. `~/.config/scanner-staging/gate.confirmed` exists, which is a dated statement that
   the Cloudflare filter below was created.

`--force` overrides 4 and shouts while doing it. `close` always works, needs no
confirmation, and proves itself by re-probing the hostname (530 = Cloudflare has no
tunnel to route to).

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
- Not yet verified: the WAF rule (needs the operator's Cloudflare session — the
  dashboard refuses automated evaluation on that page), and therefore the tunnel has
  never been left open.

## Related

- `ops/db-edge/` — the *database* edge for Vercel→Mini, a different problem with a
  different answer (PgBouncer + a router forward, parked).
- `ops/staging-db/fix-sequences.sh` — the clone defect that made staging sign-in return
  HTTP 500; read it before trusting any copied database.
