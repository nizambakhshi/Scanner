#!/usr/bin/env bash
# Open, close and inspect the public door on the staging board.
#
#   ops/staging-tunnel/staging-tunnel.sh install   put the launchd job in place (closed)
#   ops/staging-tunnel/staging-tunnel.sh open      start the tunnel — refuses if the
#                                                  gate has not been confirmed
#   ops/staging-tunnel/staging-tunnel.sh close     stop it. The door is shut again.
#   ops/staging-tunnel/staging-tunnel.sh status    what is true right now
#
# The default state is CLOSED, and `open` is deliberately argumentative. A hostname on
# the public internet that resolves to a dashboard is a second front door, so this
# script only lets that happen when two things are demonstrably true:
#
#   1. staging signs in with its OWN credential, not the production one
#      (ops/staging-tunnel/set-staging-password.sh — it checks the Keychain and the
#      app_users row, because the Mumbai clone brought production's hash with it); and
#   2. someone has confirmed the network filter, by creating the marker file it prints.
#
# Neither check is theater: (1) is read out of the Keychain and the database, and (2)
# is an explicit, dated, deletable statement that you looked at the Cloudflare rule.
set -uo pipefail

LABEL=com.scanner.staging-tunnel
DOMAIN="gui/$(id -u)"
PLIST_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/com.scanner.staging-tunnel.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
CF_CONFIG="$HOME/.cloudflared/config.yml"
HOSTNAME_STAGING=${STAGING_HOSTNAME:-scanner-staging.maktaba.co.in}
ORIGIN=http://127.0.0.1:3100
CONF_DIR="$HOME/.config/scanner-staging"
GATE_MARKER="$CONF_DIR/gate.confirmed"
KEYCHAIN_SERVICE=scanner-staging-admin
LOG="$HOME/Library/Logs/scanner-staging-tunnel.log"

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

# The checks `open` insists on, in the order a failure would be diagnosed.
preflight() {
  local ok=1 BODY hosts
  [ -x /opt/homebrew/bin/cloudflared ] || { warn "  missing  /opt/homebrew/bin/cloudflared (brew install cloudflared)"; ok=0; }
  [ -f "$CF_CONFIG" ] || { warn "  missing  $CF_CONFIG — the tunnel has no ingress definition"; ok=0; }

  if [ -f "$CF_CONFIG" ]; then
    # Comments are stripped first: this file's own header explains that the tunnel
    # cannot serve the database ports, and matching that sentence would be a false
    # alarm on the check that matters.
    BODY=$(grep -v '^[[:space:]]*#' "$CF_CONFIG")
    # The one property that makes a tunnel safe: it can only serve what is named.
    hosts=$(printf '%s\n' "$BODY" | grep -c '^[[:space:]]*- hostname:')
    if [ "$hosts" != 1 ]; then
      warn "  unexpected  $CF_CONFIG names $hosts hostnames, this script expects exactly 1"; ok=0
    fi
    printf '%s\n' "$BODY" | grep -q 'http_status:404' \
      || { warn "  unsafe  $CF_CONFIG has no http_status:404 catch-all, so an unlisted hostname would not fall through"; ok=0; }
    printf '%s\n' "$BODY" | grep -Eq 'service:.*(5432[0-9]|6543|:5432)' \
      && { warn "  unsafe  $CF_CONFIG sends a hostname to a database port; a tunnel must never carry Postgres"; ok=0; }
    printf '%s\n' "$BODY" | grep -q "$HOSTNAME_STAGING" \
      || { warn "  mismatch  $HOSTNAME_STAGING is not the hostname in $CF_CONFIG"; ok=0; }
  fi

  curl -s -o /dev/null --max-time 5 "$ORIGIN/login" \
    || { warn "  down  nothing answers on $ORIGIN — start the staging dashboard first (npm run dev:staging in ~/Desktop/Scanner-staging/app)"; ok=0; }

  security find-generic-password -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 \
    || { warn "  ungated  no $KEYCHAIN_SERVICE item in the Keychain: staging would be signing in with the PRODUCTION password. Run ops/staging-tunnel/set-staging-password.sh"; ok=0; }

  if [ ! -f "$GATE_MARKER" ]; then
    warn "  unconfirmed  no network filter has been acknowledged. Create it in Cloudflare"
    warn "               (Security → Security rules → Custom rules → Create rule) with"
    warn "                 $(printf '(http.host eq \"%s\" and ip.geoip.country ne \"IN\")' "$HOSTNAME_STAGING")"
    warn "               action Block, then record that you did:"
    warn "                 mkdir -p $CONF_DIR && echo 'waf rule created on <date>' > $GATE_MARKER"
    warn "               (or re-run with --force to open the door without it, which this script will shout about)"
    ok=0
  fi
  [ "$ok" = 1 ]
}

case "${1:-status}" in
  install)
    [ -f "$PLIST_SRC" ] || { warn "missing $PLIST_SRC"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents"
    /usr/bin/plutil -lint "$PLIST_SRC" >/dev/null || { warn "the repo plist does not parse"; exit 1; }
    cp "$PLIST_SRC" "$PLIST_DST" && chmod 644 "$PLIST_DST" || { warn "could not install $PLIST_DST"; exit 1; }
    say "installed $PLIST_DST"
    if loaded; then say "  the job is already LOADED — the tunnel is running now (close it with: $0 close)"; else say "  not loaded: the door stays closed until '$0 open' passes its preflight"; fi
    say "  run '$0 status' to see what is true, and '$0 open' to open it."
    ;;

  open)
    FORCE=0; [ "${2:-}" = "--force" ] && FORCE=1
    [ -f "$PLIST_DST" ] || { say "the job is not installed yet; installing it."; "$0" install || exit 1; }
    if [ "$FORCE" = 1 ]; then
      warn "WARNING: opening the staging board to the internet with no network filter confirmed."
      warn "         The only thing between a stranger and this dashboard is the staging password."
    else
      say "preflight:"
      preflight || { warn ""; warn "refusing to open. Fix what is listed above, or pass --force to override deliberately."; exit 1; }
      say "  all checks passed."
    fi
    loaded && { say "already running."; exit 0; }
    launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/tmp/.st_bootout_err \
      || { launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; launchctl bootstrap "$DOMAIN" "$PLIST_DST" \
             || { warn "launchd refused: $(cat /tmp/.st_bootout_err 2>/dev/null)"; exit 1; }; }
    rm -f /tmp/.st_bootout_err
    say "tunnel starting; waiting for Cloudflare to route to it"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 4
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOSTNAME_STAGING/login" 2>/dev/null)
      [ "$code" = 200 ] && { say "  https://$HOSTNAME_STAGING/login answers 200 — open it."; exit 0; }
      [ "$code" = 403 ] && { say "  https://$HOSTNAME_STAGING/login returns 403 — the tunnel is up and the WAF rule is doing its job from this address."; exit 0; }
    done
    warn "  still not answering (last HTTP code: ${code:-none}). Cloudflare returns 530 while no tunnel connection is up."
    warn "  log: $LOG"
    exit 1
    ;;

  close)
    if loaded; then
      launchctl bootout "$DOMAIN/$LABEL" && say "stopped: the launchd job is unloaded and cloudflared is gone"
    else
      say "not loaded."
    fi
    sleep 1
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOSTNAME_STAGING/login" 2>/dev/null)
    case "$code" in
      530|000|"") say "  confirmed closed from here: https://$HOSTNAME_STAGING/login -> ${code:-no answer} (Cloudflare has no tunnel to route to)" ;;
      *) warn "  WARNING: the public hostname still answers $code — something else is serving it" ;;
    esac
    ;;

  status)
    say "launchd job   : $(loaded && echo 'loaded (tunnel running)' || echo 'not loaded (closed)')"
    say "plist         : $([ -f "$PLIST_DST" ] && echo installed || echo 'not installed — run: '"$0"' install')"
    say "config        : $CF_CONFIG $([ -f "$CF_CONFIG" ] && echo '' || echo 'MISSING')"
    say "origin        : $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 $ORIGIN/login 2>/dev/null) on $ORIGIN"
    say "staging secret: $(security find-generic-password -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 && echo 'in the Keychain' || echo 'MISSING — staging still uses the production password')"
    say "gate marker   : $([ -f "$GATE_MARKER" ] && cat "$GATE_MARKER" || echo 'none — open will refuse until the WAF rule is confirmed')"
    say "public probe  : $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://$HOSTNAME_STAGING/login 2>/dev/null) (530 = closed, 200 = serving staging, 403 = filtered)"
    ;;

  preflight)
    say "preflight:"
    if preflight; then say "  all checks passed — 'open' would start the tunnel."; else exit 1; fi
    ;;

  *)
    say "usage: $0 {install|open|close|status|preflight}"; exit 2 ;;
esac
