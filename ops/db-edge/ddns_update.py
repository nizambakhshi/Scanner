#!/usr/bin/env python3
"""Keep a hostname pointing at this Mac Mini's current public IP.

Residential connections change address — the ISP renews a DHCP lease, the ONT
reboots, a line re-syncs. Vercel's DATABASE_URL is a single environment variable
that cannot follow that, and every change would otherwise mean logging into the
dashboard, editing the variable and redeploying, with the scanner broken in the
meantime. So the URL holds a hostname and this script holds the mapping.

Runs from launchd every few minutes (com.scanner.edge-ddns.plist). It is meant to
be boring: read the config, ask the internet what IP it sees for us, compare with
the last one we published, and only then call the DNS provider.

Config: ~/.config/scanner-edge/edge.env  (KEY=VALUE, mode 600; see README.md)
State:  ~/.config/scanner-edge/last_ip

Stdlib only, so /usr/bin/python3 can run it forever without a virtualenv.

Lint note: this file deliberately avoids 3.11+ conveniences (`datetime.UTC`,
match statements). The macOS system interpreter is 3.9.6, and the launchd job
runs that one on purpose — no venv to reinstall after an OS update.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONF_DIR = Path(os.environ.get("SCANNER_EDGE_CONF", "~/.config/scanner-edge")).expanduser()
CONFIG = CONF_DIR / "edge.env"
STATE = CONF_DIR / "last_ip"
LOG = CONF_DIR / "ddns.log"

# Three echoes, because a single provider outage should not look like the house
# changing its address.
IP_ECHOES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)


def log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}\n"
    sys.stdout.write(line)
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass  # stdout is captured by launchd anyway


def read_config() -> dict[str, str]:
    if not CONFIG.exists():
        log(f"no config at {CONFIG}; nothing to do (see ops/db-edge/README.md)")
        sys.exit(0)
    values: dict[str, str] = {}
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def fetch(path: str, timeout: float = 12.0) -> str:
    request = urllib.request.Request(path, headers={"User-Agent": "scanner-edge-ddns/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https hosts)
        return response.read().decode("utf-8", "replace").strip()


def current_public_ip() -> str:
    for echo in IP_ECHOES:
        try:
            candidate = fetch(echo)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"{echo} failed: {type(exc).__name__}")
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            log(f"{echo} returned a non-address: {candidate[:40]!r}")
            continue
        if address.version != 4:
            continue
        if address.is_private:
            # A private "public" address means carrier NAT: no inbound path exists,
            # so port forwarding and DDNS are both pointless until the ISP gives a
            # real address (ask for bridge/public-IP mode, or use a relay).
            raise RuntimeError(
                f"{echo} reported {address}, which is private — the connection is behind CGNAT "
                "and cannot be reached from Vercel"
            )
        return str(address)
    raise RuntimeError("no IP echo service answered")


def main() -> int:
    cfg = read_config()
    provider = cfg.get("EDGE_PROVIDER", "none").lower()
    hostname = cfg.get("EDGE_HOSTNAME", "")
    try:
        ip = current_public_ip()
    except RuntimeError as exc:
        log(f"cannot determine public IP: {exc}")
        return 1

    previous = STATE.read_text(encoding="utf-8").strip() if STATE.exists() else ""
    if not _should_publish(cfg, ip, previous):
        log(f"unchanged: {hostname or '(no hostname set)'} already points at {ip}")
        return 0

    if provider == "none":
        log(f"public IP is {ip}; EDGE_PROVIDER=none so no DNS was touched")
        STATE.write_text(ip)
        return 0

    try:
        result = _publish(provider, cfg, ip)
    except Exception as exc:  # noqa: BLE001 - a failed update must not kill the job
        log(f"{provider} update FAILED: {type(exc).__name__}: {exc}")
        return 1

    log(f"{provider} update: {previous or '(none)'} -> {ip} for {hostname or '(no hostname set)'} :: {result}")
    STATE.write_text(ip)
    return 0


def _publish(provider: str, cfg: dict[str, str], ip: str) -> str:
    if provider == "duckdns":
        domain, token = cfg.get("DUCKDNS_DOMAIN", ""), cfg.get("DUCKDNS_TOKEN", "")
        if not (domain and token):
            raise RuntimeError("DUCKDNS_DOMAIN and DUCKDNS_TOKEN are required")
        url = "https://www.duckdns.org/update?" + urllib.parse.urlencode(
            {"domains": domain, "token": token, "ip": ip}
        )
        body = fetch(url)
        if body.upper() not in ("OK", "NOCHANGE", "KO"):
            raise RuntimeError(f"duckdns answered {body[:60]!r}")
        if body.upper() == "KO":
            raise RuntimeError("duckdns rejected the token")
        return body
    if provider == "cloudflare":
        token, zone, record = cfg.get("CF_TOKEN", ""), cfg.get("CF_ZONE", ""), cfg.get("CF_RECORD", "")
        if not (token and zone and record):
            raise RuntimeError("CF_TOKEN, CF_ZONE and CF_RECORD are required")
        base = f"https://api.cloudflare.com/client/v4/zones/{zone}/dns_records"
        listed = _cf_request("GET", f"{base}?{urllib.parse.urlencode({'name': record, 'type': 'A'})}", token)
        entries = listed.get("result") or []
        if entries:
            existing = entries[0]
            if existing.get("content") == ip:
                return "already current"
            body = _cf_request("PUT", f"{base}/{existing['id']}", token,
                               {"type": "A", "name": record, "content": ip, "ttl": int(cfg.get("CF_TTL", "120"))})
        else:
            body = _cf_request("POST", base, token,
                               {"type": "A", "name": record, "content": ip, "ttl": int(cfg.get("CF_TTL", "120"))})
        if not body.get("success"):
            raise RuntimeError(str(body.get("errors")))
        return "ok"
    if provider == "noip":
        host = cfg.get("NOIP_HOSTNAME", "")
        user, secret = cfg.get("NOIP_USER", ""), cfg.get("NOIP_PASS", "")
        if not (host and user and secret):
            raise RuntimeError("NOIP_HOSTNAME, NOIP_USER and NOIP_PASS are required")
        credentials = urllib.parse.quote(f"{user}:{secret}")
        url = (f"https://{credentials}@dynupdate.no-ip.com/nic/update?"
               + urllib.parse.urlencode({"hostname": host, "myip": ip}))
        body = fetch(url)
        if "good" not in body and "nochg" not in body:
            raise RuntimeError(f"no-ip answered {body[:60]!r}")
        return body
    raise RuntimeError(f"unknown EDGE_PROVIDER {provider!r} (duckdns | cloudflare | noip | none)")


def _cf_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 (fixed api.cloudflare.com host)
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", "replace"))


def _should_publish(cfg: dict[str, str], ip: str, previous: str) -> bool:
    """Write only when it matters: the address changed, or an hourly re-check finds
    DNS disagreeing with this box.

    The re-check exists because a provider record can expire or be edited by hand;
    without it this script would keep reporting "no change" while Vercel resolved
    the old address.
    """
    if previous != ip:
        return True
    hostname = cfg.get("EDGE_HOSTNAME", "")
    if not hostname:
        return False
    marker = CONF_DIR / ".last_verify"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
            return False
        marker.touch()
        answer = json.loads(
            fetch(f"https://dns.google/resolve?name={urllib.parse.quote(hostname)}&type=A")
        )
        records = [entry["data"] for entry in answer.get("Answer", []) if entry.get("type") == 1]
    except Exception as exc:  # noqa: BLE001 - a failed check must not disable the writer
        log(f"DNS re-check skipped: {type(exc).__name__}: {exc}")
        return False
    if records and ip not in records:
        log(f"DNS for {hostname} says {', '.join(records)} but this box is {ip}; re-publishing")
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
