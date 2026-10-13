# Database edge: how Vercel reaches the Mac Mini

The scanner's database now runs as self-hosted Supabase on this Mac Mini
(`supabase_db_mohdnizam`, Postgres 17.11). Vercel's dashboard lives in a Mumbai
datacentre and the Mini lives at `192.168.1.35` behind the house router, so
production needs a path across the public internet. This directory is that path.

## What is running

| Piece | Where | Port | Notes |
| --- | --- | --- | --- |
| Supabase Postgres | Docker (OrbStack) | host `54322` → container `5432` | the real database; Studio, PostgREST and local dev keep using it |
| PgBouncer, the edge gate | launchd (`brew services`) | `6543` on every interface | the **only** port meant to be reachable from outside |
| Router | house gateway | forwards `25432` → `192.168.1.35:6543` | **one of the two steps still to do** |
| DDNS updater | launchd, every 5 min | — | republishes `EDGE_HOSTNAME` when the ISP changes the address |

Local traffic does not go through the gate: `app/.env.local` and `worker/.env`
connect to `127.0.0.1:54322` as `postgres`, which is what migrations and Studio
need. The gate is production traffic only.

One trap on that direct path: the container runs with `ssl = off`, while
`worker/worker/persistence/postgres.py` appends `sslmode=require` to any DSN that
does not already mention sslmode. So `worker/.env` must end in `?sslmode=disable`
for local runs — otherwise the pool dies with "server does not support SSL, but
SSL was required" and then `PoolTimeout` after 15s. Through the gate the opposite
applies: TLS is mandatory there, so no sslmode override is needed.

## Why a gate instead of forwarding 54322

Three reasons, all specific to this stack:

1. **The owner role must not be loggable-in from the internet.** `postgres` carries
   Supabase's short generated password and owns everything. The gate's userlist holds
   exactly one entry, `scanner_edge`, and every server connection is rewritten to that
   user — a client asking to be `postgres` is refused before it reaches the database.
2. **23 tables have row-level security on.** `postgres` reads them because it owns
   them; any other role would get zero rows and the dashboard would look like an empty
   database rather than a permissions problem. `scanner_edge` therefore carries
   `BYPASSRLS` (and `CONNECTION LIMIT 20`). That is a deliberate grant, not an
   oversight: this is a single-operator tool with no per-user policies.
3. **Vercel freezes function instances and thaws them onto dead sockets.** A bounded
   pool in front of the Mini turns that into a queue instead of a connection refusal —
   the same class of failure the Supabase session pooler produced with
   `EMAXCONNSESSION`, now under our own control. `pool_mode = session`, not
   `transaction`: `app/lib/db.ts` only disables prepared statements for URLs matching
   `pooler.supabase.com` + `:6543`, so anything else this gate could be must keep
   prepared statements safe.

TLS terminates at the gate with the self-signed certificate `install_edge.sh`
generates, which is why production sets `DATABASE_SSL=no-verify` — the wire is
encrypted, there is simply no CA to verify against. Replace the two files in
`/opt/homebrew/etc/pgbouncer/keys/` with a Let's Encrypt certificate once you have a
domain you control, then switch the variable to `require`.

## The two steps only you can do

**1. Give the Mini a hostname that follows it.** Without this, every ISP address
renewal breaks production until someone edits a Vercel variable. Either:

- DuckDNS (free, no domain to buy): sign in at duckdns.org with a GitHub/Google
  account, create `something.duckdns.org`, copy the token from the page; or
- Cloudflare (if you own a domain): add an `A` record, and create an API token with
  *Zone / DNS / Edit* permission for that zone; note the Zone ID from the dashboard.

Then write the config (mode 600, outside the repo, never committed):

```bash
mkdir -p ~/.config/scanner-edge && chmod 700 ~/.config/scanner-edge
cat > ~/.config/scanner-edge/edge.env <<'CONF'
EDGE_PROVIDER=duckdns            # duckdns | cloudflare | noip | none
EDGE_HOSTNAME=something.duckdns.org
EDGE_PUBLIC_PORT=25432
DUCKDNS_DOMAIN=something
DUCKDNS_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# Cloudflare instead:
# EDGE_PROVIDER=cloudflare
# CF_TOKEN=...  CF_ZONE=...  CF_RECORD=db.example.com
CONF
chmod 600 ~/.config/scanner-edge/edge.env
```

Check it, then load it so it survives reboots:

```bash
python3 ops/db-edge/ddns_update.py
mkdir -p ~/Library/LaunchAgents
cp ops/db-edge/com.scanner.edge-ddns.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.scanner.edge-ddns.plist
```

The updater asks three public services what address it sees for us (one outage is not
an address change), refuses to do anything if that address is private — carrier NAT
means there is no inbound path at all — and only calls the provider when the address
moved or an hourly re-check finds DNS disagreeing. Logs: `~/.config/scanner-edge/ddns.log`
and `~/Library/Logs/scanner-edge-ddns.log`.

**2. Forward the router port.** In the router's admin (usually `192.168.1.1`), add a
NAT/port-forward rule: external `25432`/TCP → `192.168.1.35` port `6543`. Do **not**
forward 54322. Some ISPs block a handful of inbound ports; 25432 is not one of them,
and if yours refuses, pick another high port and set `EDGE_PUBLIC_PORT` to match.
Give the Mini a static DHCP lease (`192.168.1.35`) so the rule stays valid.

Then:

```bash
ops/db-edge/verify.sh            # should end with "0 failed"
ops/db-edge/vercel_dsn.sh        # the two values to paste
```

## Setting production

Vercel's CLI is not logged in on this machine, so this is a dashboard action:
Project → Settings → Environment Variables → set `DATABASE_URL` and `DATABASE_SSL`
for Production (and Preview if you want it), then Production → Deployments →
Redeploy so a fresh function instance reads them. `ops/db-edge/vercel_dsn.sh
--pbcopy` puts the connection string on the clipboard.

Verify after the redeploy with the content probe that already works:

```bash
curl -s https://scanner-bullish-scanner.vercel.app/api/health   # unauthenticated: auth error JSON is correct
```
…then sign in and open `/health`: it should report `PostgreSQL 17.11`,
`databaseSsl: no-verify`, and the same 212 instruments the Mini has.

## What happens when the address changes

`EDGE_HOSTNAME` keeps resolving to the Mini within ~5 minutes, so Vercel needs no
edit — a reconnect (which `app/lib/db.ts` already does by retiring the pool on a read
deadline) picks up the new address. The one thing that does *not* follow automatically
is the certificate's SAN list; with `no-verify` that is inert, and it only becomes
relevant when you move to a real certificate.

## Diagnosing

- `ops/db-edge/verify.sh` — runs the whole checklist, including an outside host
  attempting the TCP connect (a hairpinned LAN test proves nothing, so it must be
  external).
- `/opt/homebrew/var/log/pgbouncer.log` — refusals show up here as
  `no password supplied` (wrong user) or `SSL required` (plaintext).
- `docker logs supabase_db_mohdnizam` — database-side errors.
- `security find-generic-password -a scanner_edge -s scanner-edge-postgres` — where the
  password lives. `install_edge.sh` reads it from there and never rotates it silently,
  so Vercel's copy stays valid across reinstalls.

## Rollback

Set Vercel's `DATABASE_URL` back to the Supabase Mumbai session pooler
(`aws-0-ap-south-1.pooler.supabase.com:5432`) with `DATABASE_SSL=require` and
redeploy; nothing on the Mini has to change. The two databases were identical at
migration time (312,872 one-minute candles, 212 instruments, 363 signals), so the
choice is about which one keeps receiving writes from here.
