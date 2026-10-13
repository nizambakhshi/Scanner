#!/usr/bin/env bash
# Can a machine on the other side of the world open a TCP connection to host:port?
#
#   ops/db-edge/check_tcp.sh db.example.com:25432
#   ops/db-edge/check_tcp.sh 119.42.57.64:25432      # test the router rule before
#                                                     # the hostname exists
#
# Why a third party: from inside the house the router either hairpins (and answers)
# or refuses (and hides), so a local connect proves nothing about the path Vercel
# will take. This asks check-host.net to connect from ~20 of its own nodes and
# reports what they saw. Prints one verdict line and exits 0 on "open".
#
# Verdicts, in the order they help:
#   open       at least one node completed the handshake   → the road is clear
#   refused    a node reached something that said no       → the router forwards,
#                                                              but nothing listens
#   filtered   every node timed out                        → no port forward (or the
#                                                            ISP drops inbound)
#   unknown    the probe service did not answer in a known shape
set -uo pipefail

TARGET=${1:-}
case "$TARGET" in
  *:*) ;;
  *) echo "usage: $0 <host:port>" >&2; exit 2 ;;
esac

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT

# The first GET only establishes the session cookie the POST is checked against.
curl -s -c "$JAR" -o /dev/null --max-time 15 "https://check-host.net/check-tcp"

POST=$(curl -s -b "$JAR" --max-time 25 -H "Accept: application/json" \
  -X POST "https://check-host.net/check-tcp" --data "host=${TARGET}&max_time=12")
ID=$($PY -c 'import json,sys
try: print(json.loads(sys.stdin.read()).get("request_id") or "")
except Exception: print("")' <<< "$POST")
[ -n "$ID" ] || { echo "unknown  the probe service did not accept the request: ${POST:0:120}"; exit 3; }

# Results arrive per node, and the JSON is non-empty long before any node has
# actually answered (every slot is a pending null), so "did we get a body" is not
# the right stop condition — only a node reporting a real outcome is.
RESULT=""
for _ in 1 2 3 4 5 6 7 8; do
  sleep 5
  RESULT=$(curl -s -b "$JAR" --max-time 25 -H "Accept: application/json" \
    "https://check-host.net/check-result/$ID")
  if printf '%s' "$RESULT" | grep -qE '"(error|address)"'; then break; fi
done
[ -n "$RESULT" ] || { echo "unknown  no result within the window (report: https://check-host.net/check-report/$ID)"; exit 3; }

echo "$RESULT" | $PY -c '
import json, sys
raw = sys.stdin.read()
try:
    per_node = json.loads(raw)
except Exception:
    print("unknown  the probe answered in an unexpected shape:", raw[:120]); raise SystemExit(3)
opened, refused, timeout, pending = [], [], [], []
for node, entries in per_node.items():
    # check-host.net leaves a slot as null while that node is still running, then
    # writes [{"address": …, "time": …}] for a completed connect or
    # [{"error": "…"}] for a failure. Counting a pending null as a success is how a
    # closed port gets reported open, so each shape is matched explicitly.
    if not entries or entries == [None] or all(e is None for e in entries):
        pending.append(node); continue
    entry = next((e for e in entries if isinstance(e, dict)), None)
    if entry is None:
        pending.append(node); continue
    if "error" in entry:
        text = entry["error"]
        (refused if "refus" in text.lower() else timeout).append(node)
    else:
        opened.append(node)
report = "https://check-host.net/check-report/" + sys.argv[1]
total = len(per_node)
answered = len(opened) + len(refused) + len(timeout)
# A lone node claiming success while the rest time out is a broken node, not an
# open port: one real probe from Ukraine reported a 1.7 ms handshake to a host in
# Mumbai, which is physically impossible. Requiring at least two agreeing nodes
# (and a fifth of those that answered) keeps a closed port from reading as open,
# while a genuinely forwarded port still clears easily (an open one scores 54/54).
if len(opened) >= 2 and len(opened) * 5 >= answered:
    print("open     %d of %d outside nodes completed the handshake" % (len(opened), total))
    raise SystemExit(0)
if refused:
    print("refused  %d nodes reached the address and were refused — the router IS forwarding, but nothing is listening there (check pgbouncer on 6543 and the NAT target)" % len(refused))
    raise SystemExit(1)
if len(opened) == 1:
    print("filtered %d nodes timed out and only 1 claimed success — a single agreeing node is a broken probe, not an open port; confirm at %s" % (len(timeout), report))
    raise SystemExit(1)
if timeout:
    print("filtered %d of %d nodes timed out — the router is not forwarding, or the ISP drops inbound connections" % (len(timeout), total))
    raise SystemExit(1)
print("unknown  no node has answered yet (%d pending) — see %s" % (len(pending), report))
raise SystemExit(3)
' "$ID"
status=$?
echo "report   https://check-host.net/check-report/$ID"
exit $status
