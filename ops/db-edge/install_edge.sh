#!/usr/bin/env bash
# Install / repair the Scanner database edge on this Mac Mini.
#
# Idempotent: run it after `supabase stop` + `supabase start`, after a brew
# upgrade, or on a second machine. It owns four things:
#   1. the scanner_edge role (password lives in the login Keychain, never in git)
#   2. a TLS certificate for pgbouncer (self-signed; swap in Let's Encrypt later)
#   3. /opt/homebrew/etc/pgbouncer.ini + userlist.txt, from this directory
#   4. the pgbouncer launchd job, so it survives reboots
#
# Nothing here touches the `postgres` role or the Supabase containers' internals:
# Studio, PostgREST and local development keep working exactly as before.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PBCONF=/opt/homebrew/etc/pgbouncer
KEYCHAIN_SERVICE=scanner-edge-postgres
DB_CONTAINER=supabase_db_mohdnizam
EDGE_HOSTNAME=${EDGE_HOSTNAME:-}

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

[ -x /opt/homebrew/bin/pgbouncer ] || fail "pgbouncer is not installed: brew install pgbouncer"
docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$" \
  || fail "the Supabase database container (${DB_CONTAINER}) is not running"
docker ps --format '{{.Names}}\t{{.Ports}}' | grep "^${DB_CONTAINER}" | grep -q "54322->5432" \
  || say "note: ${DB_CONTAINER} does not publish 54322->5432; check 'docker ps' before trusting pgbouncer's upstream"

# ---- 1. the edge role -------------------------------------------------------
# Generate once, keep in the Keychain. pgbouncer's userlist is derived from it, so
# re-running this script never rotates the password behind Vercel's back.
if ! security find-generic-password -a scanner_edge -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  say "creating scanner_edge password in the login Keychain"
  security add-generic-password -a scanner_edge -s "$KEYCHAIN_SERVICE" \
    -w "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)" -U \
    || fail "could not write to the login Keychain"
fi
EDGE_PW=$(security find-generic-password -a scanner_edge -s "$KEYCHAIN_SERVICE" -w) \
  || fail "could not read the scanner_edge password from the Keychain"

# Create/repair the role first, then its password: a fresh stack has neither.
docker exec "$DB_CONTAINER" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -tA <<'SQL' >/dev/null \
  || fail "could not create or grant scanner_edge"
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scanner_edge') THEN
    CREATE ROLE scanner_edge LOGIN BYPASSRLS CONNECTION LIMIT 20;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO scanner_edge;
GRANT ALL ON ALL TABLES IN SCHEMA public TO scanner_edge;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO scanner_edge;
-- New tables come from supabase/migrations, always created by `postgres`.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO scanner_edge;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO scanner_edge;
SQL

# The password goes in through stdin, never argv, so it cannot show up in a host
# or container process list.
escaped=${EDGE_PW//\'/\'\'}
printf "ALTER ROLE scanner_edge WITH LOGIN BYPASSRLS CONNECTION LIMIT 20 PASSWORD '%s';\n" "$escaped" \
  | docker exec -i "$DB_CONTAINER" psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 -tA >/dev/null \
  || fail "could not set the scanner_edge password"
unset escaped

# ---- 2. TLS certificate ----------------------------------------------------
# Self-signed is enough for DATABASE_SSL=no-verify (the driver still gets an
# encrypted channel). With a real domain, replace these two files with a Let's
# Encrypt cert and switch Vercel to DATABASE_SSL=require.
mkdir -p "$PBCONF/keys"
if [ ! -s "$PBCONF/keys/server.crt" ] || [ ! -s "$PBCONF/keys/server.key" ]; then
  san="DNS:localhost,IP:127.0.0.1"
  [ -n "$EDGE_HOSTNAME" ] && san="$san,DNS:$EDGE_HOSTNAME"
  wan=$(curl -s --max-time 6 https://api.ipify.org || true)
  [ -n "$wan" ] && san="$san,IP:$wan"
  say "generating a self-signed certificate (SAN: $san)"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$PBCONF/keys/server.key" -out "$PBCONF/keys/server.crt" \
    -days 825 -subj "/CN=${EDGE_HOSTNAME:-scanner-mini.local}" \
    -addext "subjectAltName=$san" >/dev/null 2>&1 || fail "openssl could not create the certificate"
fi
chmod 600 "$PBCONF/keys/server.key"

# ---- 3. pgbouncer configuration -------------------------------------------
# Only scanner_edge may authenticate here. A client that asks for `postgres` gets
# "no password supplied"/rejected, because no such entry exists in this file —
# that is the whole reason the edge exists.
umask 077
printf '"scanner_edge" "%s"\n' "$EDGE_PW" > "$PBCONF/userlist.txt" \
  || fail "could not write $PBCONF/userlist.txt"
unset EDGE_PW

install -m 644 "$HERE/pgbouncer.ini" "$PBCONF/pgbouncer.ini" \
  || fail "could not install pgbouncer.ini"

# ---- 4. the service ---------------------------------------------------------
# A leftover foreground pgbouncer from debugging would hold 6543 and make the
# launchd job fail to bind, so clear it first.
pkill -f 'pgbouncer -v' 2>/dev/null && sleep 1
brew services list | grep -q '^pgbouncer' && brew services stop pgbouncer >/dev/null 2>&1
brew services start pgbouncer || fail "brew services could not start pgbouncer"

sleep 2
lsof -nP -iTCP:6543 -sTCP:LISTEN >/dev/null 2>&1 \
  || { tail -20 /opt/homebrew/var/log/pgbouncer.log 2>/dev/null; fail "pgbouncer is not listening on 6543"; }

say "edge installed: pgbouncer on :6543 -> Supabase db on :54322, user scanner_edge"
say "now run: $(cd "$HERE" && pwd)/verify.sh"
