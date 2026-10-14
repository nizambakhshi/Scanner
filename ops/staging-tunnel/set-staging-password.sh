#!/usr/bin/env bash
# Give the staging board a sign-in password of its own.
#
# Why this is a script and not an env edit: `bootstrapAdmin()` in
# app/lib/auth/users.ts inserts from `AUTH_ADMIN_EMAIL` + `AUTH_ADMIN_PASSWORD_HASH`
# only while `app_users` is EMPTY. The Mini's `app_users` arrived inside the Mumbai
# clone with one row already in it, so the staging board authenticates against THAT
# row — which is the production password. Editing the environment changes nothing
# until the row changes, and that is what this script does.
#
# Where the secret lives: the login Keychain, item `scanner-staging-admin`
# (account = the operator's email). It is never printed, never written to any file,
# never passed in argv, and never in git. To put it on the clipboard:
#
#   security find-generic-password -s scanner-staging-admin -w | pbcopy
#
# Re-running is safe: an existing Keychain password is reused rather than regenerated,
# so this never rotates the credential behind your back. Pass --rotate to replace it
# deliberately, and --rotate-secret to also re-key AUTH_SESSION_SECRET (which logs
# every existing staging session out).
set -uo pipefail

KEYCHAIN_SERVICE=scanner-staging-admin
APP_DIR="$HOME/Desktop/Scanner-staging/app"
ENV_FILE="$APP_DIR/.env.local"
DB_CONTAINER=supabase_db_mohdnizam
STAGING_ORIGIN=http://127.0.0.1:3100

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

ROTATE=0; ROTATE_SECRET=0
for a in "$@"; do
  case "$a" in
    --rotate) ROTATE=1 ;;
    --rotate-secret) ROTATE_SECRET=1 ;;
    *) fail "unknown argument: $a (use --rotate and/or --rotate-secret)" ;;
  esac
done

[ -d "$APP_DIR" ] || fail "$APP_DIR does not exist — the staging worktree is where this script's target lives"
[ -f "$ENV_FILE" ] || fail "$ENV_FILE is missing"
[ -x /opt/homebrew/bin/node ] || fail "node not found at /opt/homebrew/bin/node"
docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$" \
  || fail "the Mini's database container (${DB_CONTAINER}) is not running"

EMAIL=$(grep -m1 '^AUTH_ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2- | tr -d "\"'")
[ -n "$EMAIL" ] || fail "AUTH_ADMIN_EMAIL is not set in $ENV_FILE"

# ---- 1. the password: generated once, then only ever read back ----------------
if security find-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 && [ "$ROTATE" = 0 ]; then
  say "reusing the existing staging password from the Keychain (item: $KEYCHAIN_SERVICE)"
else
  PW=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  [ "${#PW}" = 24 ] || fail "could not draw 24 characters from /dev/urandom"
  security add-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" -w "$PW" -U \
    || fail "could not write to the login Keychain"
  say "stored a new 24-character staging password in the Keychain (item: $KEYCHAIN_SERVICE)"
fi
PW=$(security find-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" -w) \
  || fail "could not read the staging password back from the Keychain"

# ---- 2. hash it with the app's own script, so the parameters cannot drift ------
# The plaintext travels through a pipe only; hash-password.mjs reads it from stdin.
HASH=$(printf '%s\n' "$PW" | (cd "$APP_DIR" && /opt/homebrew/bin/node scripts/hash-password.mjs) 2>/dev/null \
  | grep -m1 '^AUTH_ADMIN_PASSWORD_HASH=' | cut -d= -f2-)
[ -n "$HASH" ] || fail "could not produce a hash — check that $APP_DIR/scripts/hash-password.mjs runs"
case "$HASH" in scrypt\$*) ;; *) fail "unexpected hash shape, refusing to write it anywhere" ;; esac

# ---- 3. the row is the truth: replace the hash the staging board verifies against
# The hash goes to psql through stdin, never argv, so it cannot appear in a process
# list. -q keeps psql's own chatter out of this transcript; only the marker is shown.
docker exec -i "$DB_CONTAINER" psql -U postgres -d postgres -q -tA -v ON_ERROR_STOP=1 <<SQL >/tmp/.ssp_marker 2>/tmp/.ssp_err \
  || { cat /tmp/.ssp_err >&2; fail "could not update app_users on the Mini"; }
UPDATE app_users SET password_hash = '$HASH' WHERE lower(email) = lower('$EMAIL');
SELECT 'verified row count: ' || count(*) FROM app_users
 WHERE lower(email) = lower('$EMAIL') AND password_hash = '$HASH' AND is_active;
SQL
grep -v '^UPDATE' /tmp/.ssp_marker | sed 's/^/  /'
grep -q 'verified row count: 1' /tmp/.ssp_marker || fail "the row did not end up holding the new hash"
rm -f /tmp/.ssp_marker /tmp/.ssp_err

# ---- 4. keep the environment consistent with the row ---------------------------
# Matters for the day the table is rebuilt empty: then the bootstrap must not fall
# back to the production password either.
SECRET=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 48)
TMP=$(mktemp)
chmod 600 "$TMP"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    AUTH_ADMIN_PASSWORD_HASH=*) printf 'AUTH_ADMIN_PASSWORD_HASH=%s\n' "$HASH" >> "$TMP" ;;
    AUTH_SESSION_SECRET=*)
      if [ "$ROTATE_SECRET" = 1 ]; then printf 'AUTH_SESSION_SECRET=%s\n' "$SECRET" >> "$TMP"
      else printf '%s\n' "$line" >> "$TMP"; fi ;;
    *) printf '%s\n' "$line" >> "$TMP" ;;
  esac
done < "$ENV_FILE"
mv "$TMP" "$ENV_FILE" && chmod 600 "$ENV_FILE" || fail "could not rewrite $ENV_FILE"
say "wrote the new hash into $ENV_FILE$( [ "$ROTATE_SECRET" = 1 ] && say ' (and re-keyed AUTH_SESSION_SECRET — staging sessions are now invalid)' )"
say "note: the running staging process still holds the OLD AUTH_SESSION_SECRET; restart it for that to take effect."

# ---- 5. prove the staging board accepts the Keychain password ------------------
# One deliberate wrong-password request, to show the store is really being consulted.
curl -s -o /dev/null -w '  wrong password  -> HTTP %{http_code} (want 401)\n' --max-time 15 \
  -H 'content-type: application/json' \
  --data-binary @- "$STAGING_ORIGIN/api/session" <<< '{"email":"'"$EMAIL"'","password":"not-the-staging-password-0123456789"}'
security find-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" -w | tr -d '\r\n' \
  | { printf '{"email":"%s","password":"' "$EMAIL"; cat; printf '"}'; } \
  | curl -s -o /tmp/.ssp_body -D /tmp/.ssp_head -w '  right password  -> HTTP %{http_code} (want 200)\n' --max-time 20 \
      -H 'content-type: application/json' --data-binary @- "$STAGING_ORIGIN/api/session"
grep -qi '^set-cookie: scanner_session=' /tmp/.ssp_head \
  && say "  the response carried a session cookie" \
  || say "  WARNING: no scanner_session cookie in the response"
grep -qo '"ok":true' /tmp/.ssp_body && say "  and the body said ok:true"
rm -f /tmp/.ssp_body /tmp/.ssp_head
say ""
say "Staging now signs in with its own credential. Copy it when you need it:"
say "  security find-generic-password -s $KEYCHAIN_SERVICE -w | pbcopy"
