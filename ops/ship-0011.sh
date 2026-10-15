#!/usr/bin/env bash
# Ship migration 0011 and the intraday-RSI build to production, in the order that is
# survivable, with the clock as a gate.
#
#   ops/ship-0011.sh check              read-only: what is true right now (safe any time)
#   ops/ship-0011.sh migrate            apply 0011 to the live database
#   ops/ship-0011.sh verify             assert the post-migration shape
#   ops/ship-0011.sh push               git push origin main, then watch CI
#   ops/ship-0011.sh probe              prove the deployed board is the new build
#   ops/ship-0011.sh worker             restart the LIVE worker on the new code
#   ops/ship-0011.sh all                migrate → verify → push → probe → worker
#
# Why a script instead of doing it by hand at 15:40:
#
# The three moves have a hard order and one of them is irreversible-ish.
#
#   * The deployed app will SELECT `rsi_5m` and `rsi_state` from v_current_watchlist.
#     Those columns exist only after 0011, so migrating first is not a preference —
#     the board shows an error panel on every page if the app lands before the DDL.
#   * The currently running worker hand-writes `BULLISH`/`BEARISH` into
#     stock_details.rsi_bias. 0011 translates those words and replaces the CHECK with
#     one that rejects them. Between the migration and the worker restart, a process
#     on the old code that classifies RSI would fail its write. The classification runs
#     at session start (09:15), so the window is only dangerous if it is left open
#     overnight — which is why `worker` is part of `all` and not a note in a README.
#   * 0011 is NOT atomic on this path: scanner-migrate strips BEGIN/COMMIT and sends
#     one statement per round trip (that is what makes the managed pooler survivable).
#     A dropped connection mid-file leaves a half-applied file that is NOT recorded, so
#     re-running re-sends everything — which is why every DROP in the file says
#     IF EXISTS and every statement is either idempotent (CREATE OR REPLACE VIEW, an
#     UPDATE that only translates words that no longer exist once translated) or
#     skipped by the runner when the object "already exists". Re-running the file after
#     a failure is the intended recovery, not a gamble.
#
# The clock is not decoration. This machine is the trading desk: the live worker is
# writing candles right now, and a failed migration at 11:00 is a broken board for the
# rest of the session. `migrate` and `worker` refuse before 15:35 IST unless you pass
# --anyway.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_DIR="$ROOT/worker"
APP_DIR="$ROOT/app"
ENV_FILE="$WORKER_DIR/.env"
MIGRATIONS="$ROOT/supabase/migrations"
NEW_FILE=0011_rsi_entry.sql
WORKER_LOG=/tmp/live-worker.log
MARKET_CLOSED_AT=1535

# ── the database, without ever printing the credential ────────────────────────
dsn() {
  [[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; return 1; }
  local raw
  raw=$(awk -F= '/^DATABASE_URL=/{sub(/^DATABASE_URL=/,""); gsub(/^"|"$/,""); print; exit}' "$ENV_FILE")
  [[ -n "$raw" ]] || { echo "no DATABASE_URL in $ENV_FILE" >&2; return 1; }
  # the worker forces TLS itself; psql has to be told.
  if [[ "$raw" != *sslmode* ]]; then
    if [[ "$raw" == *\?* ]]; then raw+="&sslmode=require"; else raw+="?sslmode=require"; fi
  fi
  printf '%s' "$raw"
}

# q "<sql>" — one statement, tuples only, credential scrubbed out of any error.
q() {
  local dsn_value; dsn_value=$(dsn) || return 1
  psql "$dsn_value" -Atc "$1" 2>&1 | sed -E 's#(://)[^@/]*@#\1***:***@#g'
}

mask() { sed -E 's#(://)[^@/]*@#\1***:***@#g'; }

clock_state() {
  local hm weekday
  hm=$(TZ=Asia/Kolkata date +%H%M)
  weekday=$(TZ=Asia/Kolkata date +%u)
  if [[ "$weekday" -le 5 && "$hm" -lt "$MARKET_CLOSED_AT" ]]; then
    return 1
  fi
  return 0
}

# ── commands ──────────────────────────────────────────────────────────────────
check() {
  echo "== which database answers"
  dsn | mask; echo
  q "SELECT current_database(), inet_server_addr(), current_setting('server_version_num')::int"

  echo
  echo "== migration state (nothing written)"
  (cd "$WORKER_DIR" && .venv/bin/python -m worker.scripts.migrate \
      --database-url "$(dsn)" --dir "$MIGRATIONS" --status 2>&1 | mask)

  echo
  echo "== v_current_watchlist, last three columns today"
  q "SELECT ordinal_position, column_name FROM information_schema.columns
       WHERE table_schema='public' AND table_name='v_current_watchlist'
       ORDER BY ordinal_position DESC LIMIT 3"

  echo
  echo "== indicator_snapshot columns 0011 adds"
  q "SELECT column_name FROM information_schema.columns
       WHERE table_schema='public' AND table_name='indicator_snapshots'
         AND column_name IN ('rsi_5m','rsi_state') ORDER BY column_name"

  echo
  echo "== the vocabulary sitting in stock_details right now"
  q "SELECT coalesce(rsi_bias,'(null)') AS bias, count(*) FROM stock_details
       GROUP BY 1 ORDER BY 2 DESC"

  echo
  echo "== the CHECK that 0011 replaces"
  q "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
       WHERE conrelid='stock_details'::regclass AND contype='c'
         AND pg_get_constraintdef(oid) LIKE '%rsi_bias%'"

  echo
  echo "== the live worker: which tree, started when"
  local pid
  pid=$(pgrep -f "scanner-worker" | while read -r p; do
          [[ "$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2)}')" == "$WORKER_DIR" ]] && echo "$p"
        done | head -1)
  if [[ -n "$pid" ]]; then
    echo "pid $pid  cwd $WORKER_DIR  started $(ps -o lstart= -p "$pid")"
    echo "it cannot have code committed after its start time — compare:"
    git -C "$ROOT" log -1 --format='   newest commit  %h %ad  %s' --date='format:%Y-%m-%d %H:%M'
  else
    echo "no scanner-worker process rooted in $WORKER_DIR"
  fi

  echo
  if clock_state; then
    echo "== clock: market closed (or a weekend) — migrate/worker will run"
  else
    echo "== clock: BEFORE $(printf '%02d:%02d' $((10#${MARKET_CLOSED_AT}/100)) $((10#${MARKET_CLOSED_AT}%100))) IST on a weekday."
    echo "   migrate and worker will refuse. check is read-only and fine to run now."
  fi
}

migrate() {
  if [[ "${1:-}" != "--anyway" ]]; then
    clock_state || { echo "REFUSED: the session may still be running. Pass --anyway to override." >&2; return 1; }
  fi
  echo "== dry run first"
  (cd "$WORKER_DIR" && .venv/bin/python -m worker.scripts.migrate \
      --database-url "$(dsn)" --dir "$MIGRATIONS" --dry-run 2>&1 | mask)
  echo
  echo "== applying"
  (cd "$WORKER_DIR" && .venv/bin/python -m worker.scripts.migrate \
      --database-url "$(dsn)" --dir "$MIGRATIONS" 2>&1 | mask)
  echo
  echo "== if the line above is not a clean 'applied', re-run this command."
  echo "   The file is registered only when every statement succeeded, so a partial"
  echo "   pass left nothing recorded; every statement in it tolerates a second visit."
}

verify() {
  echo "== 0011 recorded"
  q "SELECT name, left(checksum, 12) AS checksum, applied_at FROM schema_migrations
       WHERE name LIKE '0011%'"
  echo
  echo "== the view's last two columns are the new ones"
  q "SELECT ordinal_position, column_name FROM information_schema.columns
       WHERE table_schema='public' AND table_name='v_current_watchlist'
       ORDER BY ordinal_position DESC LIMIT 2"
  echo
  echo "== the snapshot carries both intraday columns"
  q "SELECT column_name, data_type, is_nullable FROM information_schema.columns
       WHERE table_schema='public' AND table_name='indicator_snapshots'
         AND column_name IN ('rsi_5m','rsi_state') ORDER BY column_name"
  echo
  echo "== no row kept an old word, and the two CHECKs list the three new ones"
  q "SELECT count(*) AS stale_words FROM stock_details
       WHERE rsi_bias IN ('BULLISH','BEARISH') OR last_alert_bias IN ('BULLISH','BEARISH')"
  q "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
       WHERE conname IN ('stock_details_rsi_bias_check','stock_details_last_alert_bias_check',
                         'indicator_snapshots_rsi_state_check')
       ORDER BY conname"
  echo
  echo "== the signal CHECK knows the two alert names the worker raises"
  q "SELECT pg_get_constraintdef(oid) LIKE '%RSI_ENTRY%' AS has_entry,
            pg_get_constraintdef(oid) LIKE '%RSI_OVERBOUGHT%' AS has_overbought
       FROM pg_constraint WHERE conname='signals_signal_type_check'"
  echo
  echo "== the board's own read"
  q "SELECT count(*) AS watchlist_rows FROM v_current_watchlist"
  q "SELECT symbol, rsi_5m, rsi_state FROM v_current_watchlist WHERE rsi_state IS NOT NULL LIMIT 3"
  echo "   (rsi_state NULL everywhere is correct: the column was empty until the"
  echo "    restarted worker closes its first five-minute bucket tomorrow)"
}

push() {
  echo "== pushing main"
  git -C "$ROOT" push origin main
  echo
  echo "== CI"
  gh run watch "$(gh run list --repo nizambakhshi/Scanner --branch main --limit 1 --json databaseId -q '.[0].databaseId')" \
      --repo nizambakhshi/Scanner --exit-status || echo "CI is not green — do NOT restart the worker on a build that failed its own tests."
}

probe() {
  # What this can and cannot prove without a Vercel login. The status check ties a
  # deployment to the exact sha that was pushed, which is the part a content grep on a
  # CSS chunk can never establish; the fetch only shows that the app boots and answers.
  local sha state url
  sha=$(git -C "$ROOT" rev-parse HEAD)
  echo "== the deployment built from $sha"
  gh api "repos/nizambakhshi/Scanner/commits/$sha/status" \
      --jq '.status[] | select(.context=="Vercel") | "\(.state)  \(.target_url // "")"'
  state=$(gh api "repos/nizambakhshi/Scanner/commits/$sha/status" \
      --jq '[.status[] | select(.context=="Vercel") | .state][0] // "none"')
  url=$(gh api "repos/nizambakhshi/Scanner/commits/$sha/status" \
      --jq '[.status[] | select(.context=="Vercel") | .target_url][0] // ""')
  case "$state" in
    success) ;;
    pending) echo "   still building; re-run probe in a minute" ; return 1 ;;
    *) echo "   Vercel reported '$state' for this commit — stop here and read the deployment" >&2; return 1 ;;
  esac

  echo
  echo "== that deployment answers. /watchlist is behind the login, so an unauthenticated"
  echo "   request must be redirected to /login — a 200 there would mean auth is broken."
  if [[ -n "$url" ]]; then
    printf '%s  ' "$url"; curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$url/watchlist"
  fi
  printf 'production  '
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
    https://scanner-bullish-scanner.vercel.app/watchlist
  echo
  echo "The columns are real data only after tomorrow's session; what still needs a"
  echo "signed-in eye is the board itself — open it in the browser, do not assume it."
}

worker() {
  if [[ "${1:-}" != "--anyway" ]]; then
    clock_state || { echo "REFUSED: the session may still be running. Pass --anyway to override." >&2; return 1; }
  fi
  echo "== stopping the live worker"
  local pid
  pid=$(pgrep -f "scanner-worker" | while read -r p; do
          [[ "$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2)}')" == "$WORKER_DIR" ]] && echo "$p"
        done | head -1)
  if [[ -n "$pid" ]]; then
    kill "$pid"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null
    echo "pid $pid stopped"
  else
    echo "nothing to stop rooted in $WORKER_DIR"
  fi

  echo
  echo "== starting it on the current tree, log appended to $WORKER_LOG"
  # -u DATABASE_URL: config.py loads worker/.env with override=False, so a stray
  # DATABASE_URL in this shell would outrank the file the operator edits. The live
  # credential must come from worker/.env and nowhere else.
  (cd "$WORKER_DIR" && env -u DATABASE_URL -u SCAN_MODE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
      nohup .venv/bin/scanner-worker >>"$WORKER_LOG" 2>&1 &)
  sleep 12
  echo
  echo "== what the new process says about itself"
  tail -n 60 "$WORKER_LOG" | grep -a -E \
    "worker_configuration|telegram_credentials_applied|alert_strateg|rsi|strategy_defaults" | tail -8
  echo
  echo "Expect: a worker_configuration line with the NEW pid, and no traceback."
  echo "The five-minute RSI verdict is taken when a bucket closes, so the first"
  echo "rsi_state values appear during tomorrow's session, not tonight."
}

case "${1:-check}" in
  check)   shift; check ;;
  migrate) shift; migrate "${1:-}" ;;
  verify)  shift; verify ;;
  push)    shift; push ;;
  probe)   shift; probe ;;
  worker)  shift; worker "${1:-}" ;;
  all)     shift; migrate && verify && push && probe && worker ;;
  *)       sed -n '2,12p' "$0"; exit 2 ;;
esac
