#!/usr/bin/env bash
# Print the two environment variables Vercel needs to reach the Mini.
#
# The password lives in the login Keychain and is echoed here on purpose: pasting
# it into the Vercel dashboard is the only way to set production, and this is the
# operator's own terminal. It is never written to a file, and never into git —
# which is why nothing in ops/db-edge/ contains a credential.
#
#   ops/db-edge/vercel_dsn.sh            print the block
#   ops/db-edge/vercel_dsn.sh --pbcopy   put DATABASE_URL alone on the clipboard
set -uo pipefail

CONF=${SCANNER_EDGE_CONF:-$HOME/.config/scanner-edge}
EDGE_ENV="$CONF/edge.env"

HOST=$(grep -m1 '^EDGE_HOSTNAME=' "$EDGE_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"'"'")
PORT=$(grep -m1 '^EDGE_PUBLIC_PORT=' "$EDGE_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"'"'")
PORT=${PORT:-25432}

if [ -z "$HOST" ]; then
  echo "EDGE_HOSTNAME is not set in $EDGE_ENV." >&2
  echo "An IP is not a substitute: the moment the ISP renews its lease, production is dead" >&2
  echo "until someone edits an environment variable. Set the hostname first (README.md)." >&2
  exit 1
fi

PW=$(security find-generic-password -a scanner_edge -s scanner-edge-postgres -w) || {
  echo "no scanner_edge password in the login Keychain; run install_edge.sh" >&2; exit 1; }

DSN="postgresql://scanner_edge:${PW}@${HOST}:${PORT}/postgres"

if [ "${1:-}" = "--pbcopy" ]; then
  printf '%s' "$DSN" | pbcopy
  echo "DATABASE_URL is on the clipboard."
  exit 0
fi

cat <<EOF
Vercel → Settings → Environment Variables (Production, Preview, Development):

  DATABASE_URL   ${DSN}
  DATABASE_SSL   no-verify

DATABASE_SSL=no-verify because the edge certificate is self-signed: the channel is
still encrypted, the driver just does not have a CA to check it against. app/lib/db.ts
accepts this value, and /health labels it, precisely so a self-hosted proxy is not
silently mistaken for a verified connection. With a real domain and a Let's Encrypt
certificate, switch to require.

Then redeploy (Production → Deployments → ... → Redeploy) so a fresh function picks
the value up; existing instances keep the old pool until they are replaced.
EOF
