#!/usr/bin/env bash
# Is the database reachable the way Vercel needs it to be? Checks, in the order a
# failure should be diagnosed: the container, the gate, the gate's rules, the
# hostname, then the road from outside the house.
#
# Read-only. Safe to run any time, including while the scanner is mid-session.
set -uo pipefail

CONF=${SCANNER_EDGE_CONF:-$HOME/.config/scanner-edge}
EDGE_ENV="$CONF/edge.env"
PSQL=$(command -v psql || echo /opt/homebrew/opt/postgresql@17/bin/psql)
[ -x "$PSQL" ] || PSQL=/opt/homebrew/bin/psql

pass=0; warn=0; fail=0
ok()   { printf '  ok    %s\n' "$*"; pass=$((pass+1)); }
no()   { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }
note() { printf '  note  %s\n' "$*"; warn=$((warn+1)); }
section() { printf '\n%s\n' "$*"; }

EDGE_HOSTNAME=""; EDGE_PUBLIC_PORT=""
[ -f "$EDGE_ENV" ] && EDGE_HOSTNAME=$(grep -m1 '^EDGE_HOSTNAME=' "$EDGE_ENV" | cut -d= -f2- | tr -d '"'"'")
[ -f "$EDGE_ENV" ] && EDGE_PUBLIC_PORT=$(grep -m1 '^EDGE_PUBLIC_PORT=' "$EDGE_ENV" | cut -d= -f2- | tr -d '"'"'")
EDGE_PUBLIC_PORT=${EDGE_PUBLIC_PORT:-25432}

section "1. the Supabase container"
docker ps --format '{{.Names}} {{.Ports}}' | grep 'supabase_db' | grep -q '54322->5432' \
  && ok "supabase_db is up and publishes 54322 -> 5432" \
  || no  "supabase_db is not running with 54322 published (supabase start)"

section "2. pgbouncer, the edge gate"
lsof -nP -iTCP:6543 -sTCP:LISTEN >/dev/null 2>&1 \
  && ok "listening on 0.0.0.0:6543" \
  || no  "nothing is listening on 6543 (ops/db-edge/install_edge.sh)"
brew services info pgbouncer 2>/dev/null | grep -qi 'Running: true' \
  && ok "managed by launchd, so it comes back after a reboot" \
  || note "not running as a brew service: a reboot would leave the gate closed"

section "3. what the gate allows"
EDGE_PW=$(security find-generic-password -a scanner_edge -s scanner-edge-postgres -w 2>/dev/null) \
  || { no "no scanner_edge password in the login Keychain"; EDGE_PW=""; }
if [ -n "$EDGE_PW" ]; then
  rows=$(PGPASSWORD="$EDGE_PW" "$PSQL" -w "host=127.0.0.1 port=6543 user=scanner_edge dbname=postgres sslmode=require" \
         -Atc "select count(*) from candles_1m" 2>/dev/null)
  [ -n "$rows" ] && ok "scanner_edge + TLS reads candles_1m ($rows rows; a non-zero number also proves BYPASSRLS works on the 23 RLS tables)" \
                 || no  "scanner_edge could not read candles_1m over TLS"
  "$PSQL" -w "host=127.0.0.1 port=6543 user=postgres dbname=postgres sslmode=require" -Atc "select 1" >/dev/null 2>&1 \
    && no  "the postgres owner role got through the gate — the userlist is not restricting anyone" \
    || ok  "postgres cannot authenticate through the gate"
  PGPASSWORD="$EDGE_PW" "$PSQL" -w "host=127.0.0.1 port=6543 user=scanner_edge dbname=postgres sslmode=disable" \
    -Atc "select 1" >/dev/null 2>&1 \
    && no  "a plaintext session was accepted on the edge port" \
    || ok  "plaintext is refused; TLS is mandatory"
  unset EDGE_PW
fi

section "4. this box's public address"
WAN=$(curl -s --max-time 8 https://api.ipify.org)
if [ -z "$WAN" ]; then note "no answer from the IP echo services (offline?)"
else
  printf '  wan   %s\n' "$WAN"
  case "$WAN" in
    10.*|192.168.*|172.16.*|172.17.*|172.18.*|100.6[4-9].*|100.7[0-9].*|100.1[01][0-9].*|100.12[0-7].*)
      no "$WAN is private: carrier NAT, so no inbound path exists from Vercel" ;;
    *) ok "$WAN is a public address" ;;
  esac
fi

section "5. the hostname Vercel will use"
if [ -z "$EDGE_HOSTNAME" ]; then
  note "EDGE_HOSTNAME is not set in $EDGE_ENV — the IP-change handling is not active yet"
else
  resolved=$(/usr/bin/dig +short +time=4 A "$EDGE_HOSTNAME" 2>/dev/null | head -1)
  if [ -z "$resolved" ]; then no "$EDGE_HOSTNAME does not resolve"
  elif [ -n "$WAN" ] && [ "$resolved" = "$WAN" ]; then ok "$EDGE_HOSTNAME -> $resolved (matches this box)"
  else
    no "$EDGE_HOSTNAME -> $resolved but this box is $WAN — run ddns_update.py, or the address moved and the updater is not loaded"
  fi
fi

section "6. the road in from outside"
if [ -z "$EDGE_HOSTNAME" ]; then
  note "skipped: set EDGE_HOSTNAME first"
else
  # A third party has to do this connect: from inside the LAN the router often
  # hairpins (or refuses to), so a local test proves nothing either way.
  probe=$(/usr/bin/curl -s --max-time 15 \
    "https://api.yougetsignal.com/tools/web-sites-on-web-server/php/get-web-server-monitoring-data.php?remote_address=${EDGE_HOSTNAME}&port=${EDGE_PUBLIC_PORT}&flag=1" \
    2>/dev/null)
  case "$probe" in
    *'"portOpen":true'*|*'"accepted":true'*) ok "an outside host reached $EDGE_HOSTNAME:$EDGE_PUBLIC_PORT" ;;
    *'"portOpen":false'*|*'"accepted":false'*)
      no "an outside host could not reach $EDGE_HOSTNAME:$EDGE_PUBLIC_PORT — the router is not forwarding" ;;
    *) note "the port-check service did not answer in a known shape: ${probe:0:120}" ;;
  esac
fi

section "7. macOS firewall"
fw=$(/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null)
case "$fw" in
  *"enabled"*) note "the application firewall is on; confirm it permits pgbouncer on 6543 if step 6 fails" ;;
  *)           ok "the application firewall is off" ;;
esac

printf '\nsummary: %d ok, %d advisory, %d failed\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] || printf 'the dashboard cannot use the Mini until the failures above are gone.\n'
exit $(( fail > 0 ? 1 : 0 ))
