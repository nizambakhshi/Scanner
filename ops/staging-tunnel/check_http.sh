#!/usr/bin/env bash
# What does this URL look like from the rest of the world — and from India?
#
#   ops/staging-tunnel/check_http.sh [url] [wanted-abroad-code]
#
#   ops/staging-tunnel/check_http.sh https://scanner-staging.maktaba.co.in/login 403
#
# check-host.net fetches the URL from ~50 of its own nodes and labels each one with
# its country, so the answer can be split by vantage. That split is the whole point:
# a single local request can never tell "Cloudflare blocked this visitor" apart from
# "the tunnel was not running", and those two look identical from the couch.
#
# The readings that matter for the staging gate:
#
#   abroad 403   the WAF country rule matched and refused at the edge
#   abroad 530   the rule did NOT match — the request reached the tunnel layer and
#                found no connector
#   IN     530   correct while the tunnel is closed: Indian visitors are not blocked,
#                there is simply nothing connected yet
#   IN     403   the rule is inverted and has locked you out of your own board
#
# Run it while the tunnel is CLOSED: abroad 403 + IN 530 is then proof the gate
# discriminates, and it is the only proof available before opening the door.
#
# Exit 0 when every answered abroad node returned the wanted code AND no Indian node
# was refused with it; 1 when either half is wrong; 3 when the probe misbehaved.
set -uo pipefail

URL=${1:-https://scanner-staging.maktaba.co.in/login}
WANT=${2:-403}

PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT

# The first GET only establishes the session cookie the POST is checked against.
curl -s -c "$JAR" -o /dev/null --max-time 15 "https://check-host.net/check-http"

POST=$(curl -s -b "$JAR" --max-time 25 -H "Accept: application/json" \
  -X POST "https://check-host.net/check-http" --data "host=${URL}&max_time=15")
[ -n "$POST" ] || { echo "unknown  the probe service did not answer at all"; exit 3; }

ID=$(printf '%s' "$POST" | $PY -c 'import json,sys
try: print(json.loads(sys.stdin.read()).get("request_id") or "")
except Exception: print("")')
[ -n "$ID" ] || { echo "unknown  the probe refused the request: ${POST:0:160}"; exit 3; }

# The POST body also names every node it assigned, with its country; keep it, because
# the result rows carry only the node hostname.
printf '%s' "$POST" > /tmp/.ch_nodes.json

# Every node slot is a pending null long before any node has actually answered, so the
# stop condition has to be a real verdict, not a non-empty body.
RESULT=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  RESULT=$(curl -s -b "$JAR" --max-time 25 -H "Accept: application/json" \
    "https://check-host.net/check-result/$ID")
  if printf '%s' "$RESULT" | grep -qE '"(Forbidden|OK|Not Found|error|[0-9]{3})"'; then break; fi
done
[ -n "$RESULT" ] || { echo "unknown  no result within the window — https://check-host.net/check-report/$ID"; exit 3; }

printf '%s' "$RESULT" | $PY -c '
import json, sys
raw = sys.stdin.read()
want, url = sys.argv[1], sys.argv[2]
report = "https://check-host.net/check-report/" + sys.argv[3]
try:
    per_node = json.loads(raw)
    nodes = json.load(open("/tmp/.ch_nodes.json")).get("nodes", {})
except Exception as exc:
    print("unknown  an unexpected reply (%s): %s" % (exc, raw[:160])); raise SystemExit(3)

def place(node):
    # nodes["de1.node.check-host.net"] = ["de","Germany","Berlin","ip","AS…"]
    meta = nodes.get(node) or []
    if len(meta) >= 2:
        return meta[0].lower(), meta[1]
    return (node[:2].lower(), "?")

def verdict(entry):
    # httpcheck rows are positional: [0, seconds, "Forbidden", "403", "188.114.96.0"].
    # A failure instead comes back as [{"error": "…"}]. Reading the wrong slot is how a
    # blocked probe gets reported as an open door, so match both shapes explicitly.
    # The code is not reliably a string — one German node answered "Forbidden" with the
    # number in another slot — so take any 3-digit value, and fall back to the reason
    # phrase, which maps to a code anyway.
    reasons = {"forbidden": "403", "ok": "200", "not found": "404",
               "unauthorized": "401", "moved temporarily": "302",
               "too many requests": "429", "bad gateway": "502",
               "gateway timeout": "504", "service unavailable": "503"}
    if isinstance(entry, dict):
        return str(entry.get("error") or entry.get("code") or "pending")[:24], ""
    if isinstance(entry, list):
        code = next((str(x) for x in entry
                     if (isinstance(x, str) and x.isdigit() and len(x) == 3)
                     or (isinstance(x, int) and 100 <= x <= 599)), None)
        text = next((x for x in entry if isinstance(x, str) and not x.isdigit()), "")
        ip = next((x for x in entry if isinstance(x, str) and x.count(".") == 3), "")
        if not code:
            code = reasons.get(text.strip().lower(), text or "pending")
        return code[:24], ip
    return "pending", ""

groups, ips = {}, {}
for node, entries in sorted(per_node.items()):
    cc, country = place(node)
    cls = "IN" if cc == "in" else "abroad"
    row = entries[0] if entries and isinstance(entries[0], list) else (entries or [None])[0]
    if not entries or row is None:
        v, ip = "pending", ""
    else:
        v, ip = verdict(row)
    if ip: ips.setdefault(v, ip)
    groups.setdefault((cls, v), []).append((cc, country))

answered = sum(len(v) for k, v in groups.items() if k[1] != "pending")
if answered == 0:
    print("unknown  no node has answered yet — %s" % report); raise SystemExit(3)

print("probe    %s" % url)
for (cls, v), places in sorted(groups.items()):
    shown = ", ".join(sorted({c for _, c in places})[:6])
    extra = "" if cls == "IN" else "  e.g. %s" % shown
    print("  %-6s %-24s %3d nodes%s%s" % (
        cls, v, len(places), ("  from %s" % ips[v]) if ips.get(v) else "", extra))
print("         %d of %d nodes answered — %s" % (answered, len(per_node), report))

abroad_bad = [(v, p) for (c, v), p in groups.items()
              if c == "abroad" and v != "pending" and v != want]
in_bad = [(v, p) for (c, v), p in groups.items()
          if c == "IN" and v != "pending" and v not in ("530", "200", "502", "521", "522", "523", "524")]
saw_want = any(c == "abroad" and v == want for (c, v) in groups)
saw_in = any(c == "IN" and v != "pending" for (c, v) in groups)

if abroad_bad:
    print("NOT GATED  abroad nodes saw %s instead of %s — the country rule is not matching them" % (
        ", ".join(sorted({v for v, _ in abroad_bad})), want))
    raise SystemExit(1)
if in_bad:
    print("OVER-BLOCKED  an Indian node was refused (%s) — the rule would lock you out too" %
        ", ".join(sorted({v for v, _ in in_bad})))
    raise SystemExit(1)
if not saw_want:
    print("unknown  no abroad node answered with %s yet" % want); raise SystemExit(3)
print("GATED    every answered abroad node got %s; India was not%s" % (
    want, "" if saw_in else " (no Indian node answered — confirm your own vantage with: curl -s -o /dev/null -w \"%%{http_code}\\n\" %s)" % url))
raise SystemExit(0)
' "$WANT" "$URL" "$ID"
status=$?
rm -f /tmp/.ch_nodes.json
exit $status
