#!/usr/bin/env bash
# Install / repair the IP-change updater as a launchd job on this Mac Mini.
#
#   ops/db-edge/install_ddns.sh          copy the script, write the job, load it
#   ops/db-edge/install_ddns.sh --check  report state, change nothing
#
# Why the script is *copied* instead of run from the repository: macOS protects
# Desktop, Documents and Downloads with TCC, and a launchd job runs as a plain
# /usr/bin/python3 with no permission to read them. Pointing the plist at
# ~/Desktop/Scanner/ops/db-edge/ddns_update.py produces
#   can't open file '…/ddns_update.py': [Errno 1] Operation not permitted
# in the job's log, and nothing ever updates — silently, once per schedule,
# forever. So the running copy lives in ~/.config/scanner-edge/ next to its
# config and state, and the repository stays the source of truth.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF_DIR="$HOME/.config/scanner-edge"
RUNTIME_SCRIPT="$CONF_DIR/ddns_update.py"
PLIST_LABEL=com.scanner.edge-ddns
PLIST_SRC="$HERE/$PLIST_LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_DEST="$HOME/Library/Logs/scanner-edge-ddns.log"
DOMAIN="gui/$(id -u)"

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

check_only=0
[ "${1:-}" = "--check" ] && check_only=1

[ -f "$PLIST_SRC" ] || fail "missing $PLIST_SRC"

# ---- state ------------------------------------------------------------------
loaded=no; last_error=""
launchctl list "$PLIST_LABEL" >/dev/null 2>&1 && loaded=yes
# Only the newest line counts: an older "can't open file" is the history of a
# previous install, not a current failure.
[ -f "$LOG_DEST" ] && last_error=$(tail -1 "$LOG_DEST" | grep "can't open file" || true)
if [ -n "$last_error" ]; then
  say "job is installed but failing: $last_error"
fi

if [ "$check_only" -eq 1 ]; then
  say "script  $RUNTIME_SCRIPT $([ -f "$RUNTIME_SCRIPT" ] && echo '(present)' || echo '(MISSING)')"
  say "plist   $PLIST_DEST $([ -f "$PLIST_DEST" ] && echo '(present)' || echo '(MISSING)')"
  say "launchd $([ "$loaded" = yes ] && echo 'loaded' || echo 'NOT loaded')"
  say "config  $CONF_DIR/edge.env $([ -f "$CONF_DIR/edge.env" ] && echo '(present)' || echo '(MISSING — see README.md)')"
  exit 0
fi

# ---- install ----------------------------------------------------------------
mkdir -p "$CONF_DIR" && chmod 700 "$CONF_DIR"
install -m 644 "$HERE/ddns_update.py" "$RUNTIME_SCRIPT" \
  || fail "could not copy ddns_update.py to $RUNTIME_SCRIPT"
mkdir -p "$HOME/Library/LaunchAgents"

# The repo plist documents the schedule; the installed one must point at the
# copied script for the reason above, so the path is rewritten rather than copied.
sed "s|$HERE/ddns_update.py|$RUNTIME_SCRIPT|g" "$PLIST_SRC" > "$PLIST_DEST" \
  || fail "could not write $PLIST_DEST"
grep -q "$RUNTIME_SCRIPT" "$PLIST_DEST" \
  || fail "the installed plist does not reference $RUNTIME_SCRIPT — check the path in $PLIST_SRC"

[ -f "$CONF_DIR/edge.env" ] || {
  say "note: $CONF_DIR/edge.env does not exist yet, so the updater will log"
  say "      'no config; nothing to do' every five minutes until you add it (README.md)."
}

# Reload, so an edited plist takes effect instead of the cached copy.
launchctl bootout "$DOMAIN/$PLIST_LABEL" >/dev/null 2>&1
launchctl bootstrap "$DOMAIN" "$PLIST_DEST" 2>/dev/null \
  || launchctl load -w "$PLIST_DEST" 2>/dev/null \
  || fail "launchctl could not load $PLIST_DEST"
launchctl kickstart -k "$DOMAIN/$PLIST_LABEL" >/dev/null 2>&1 || true

# ---- verify -----------------------------------------------------------------
sleep 4
launchctl list "$PLIST_LABEL" >/dev/null 2>&1 || fail "the job is not loaded after bootstrap"
if ! tail -3 "$LOG_DEST" 2>/dev/null | grep -q "2026-\|public IP\|unchanged\|no config"; then
  say "note: no fresh log line in $LOG_DEST yet; check it after one schedule tick"
fi
say "updater installed: $PLIST_LABEL, every 5 minutes, running $RUNTIME_SCRIPT"
tail -2 "$LOG_DEST" 2>/dev/null | sed 's/^/  log: /'
say "next: fill in EDGE_HOSTNAME and a provider token in $CONF_DIR/edge.env,"
say "      then forward the router port (README.md) and run verify.sh."
