"""Fill the fundamentals columns of ``stock_details``: P/E, market cap, listing date.

Two public sources, each authoritative for what it provides (probed 2026-10-12):

* **Yahoo Finance** (``query1.finance.yahoo.com``) — trailing P/E and market
  cap, fetched through the crumb flow with a browser-impersonating client
  (``curl_cffi``). Values verified against the operator's own reference
  screenshot for RELIANCE/TCS on 2026-10-12. Market cap arrives in rupees and
  is stored in **crore** (÷10⁷) to match the dashboard column.
* **NSE archives** (``archives.nseindia.com/content/equities/EQUITY_L.csv``,
  plain httpx) — the official equity list, the only usable source for the
  listing date. The NSE *API* is Akamai-blocked from this host; the archives
  CSV is not.

Neither source is asked to fill anything the other owns, and the store's
upsert merges per column (a Yahoo-only run can never blank a listing date).
A symbol both sources miss is not written at all — an absent profile row is
the honest state, not a row of nulls that would erase an older good value.

P/E is deliberately taken as Yahoo's ``trailingPE``: loss-making companies
return nothing, and the column then stays NULL rather than showing an invented
number. Yahoo sends no documented rate limit for this endpoint; the script
paces batches regardless, and a failed batch is retried once before its
symbols are reported as missed.

Usage::

    python -m worker.scripts.sync_profiles                 # both sources
    python -m worker.scripts.sync_profiles --source yahoo
    python -m worker.scripts.sync_profiles --dry-run       # fetch, write nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import date, datetime

import httpx

from ..config import WorkerSettings, load_settings
from ..logging_setup import configure_logging, get_logger, scrub
from ..models import StockProfile
from ..persistence.memory import InMemoryStore
from ..persistence.postgres import PostgresStore
from ..persistence.store import ScannerStore

log = get_logger(__name__)

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_SYMBOL_SUFFIX = ".NS"
YAHOO_BATCH_SIZE = 40
YAHOO_PAUSE_SECONDS = 1.2
#: 1 crore = 10^7 rupees; Yahoo reports market cap in rupees.
RUPEES_PER_CRORE = 10_000_000.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_store(settings: WorkerSettings) -> ScannerStore:
    return PostgresStore(settings.database_url) if settings.database_url else InMemoryStore()


def parse_listing_date(value: str) -> date | None:
    """``29-NOV-1995`` → ``date``; anything unparseable stays ``None``."""
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_equity_list(csv_text: str) -> dict[str, date]:
    """SYMBOL → listing date from NSE's EQUITY_L.csv.

    The file is CSV with a header row; the columns relied on here are
    ``SYMBOL`` and ``DATE OF LISTING`` (verified 2026-10-12). Rows that fail
    to parse are skipped by name, not silently: the caller logs the count of
    symbols the NSE list did not cover.
    """
    lines = csv_text.splitlines()
    if not lines:
        return {}
    header = [column.strip().upper() for column in lines[0].split(",")]
    try:
        symbol_at = header.index("SYMBOL")
        listing_at = header.index("DATE OF LISTING")
    except ValueError:
        log.error("equity_list_unexpected_header", header=lines[0][:200])
        return {}
    out: dict[str, date] = {}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) <= max(symbol_at, listing_at):
            continue
        symbol = parts[symbol_at].strip().upper()
        listing = parse_listing_date(parts[listing_at])
        if symbol and listing is not None:
            out[symbol] = listing
    return out


async def fetch_equity_list(client: httpx.AsyncClient) -> dict[str, date]:
    """The NSE archives CSV; best-effort cookie warm-up first, then the file."""
    try:
        await client.get(
            "https://www.nseindia.com/",
            headers=BROWSER_HEADERS,
            timeout=20.0,
        )
    except httpx.HTTPError:
        pass  # the archives host does not require the cookie; this is a nicety
    try:
        response = await client.get(
            NSE_EQUITY_LIST_URL,
            headers=BROWSER_HEADERS,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        log.warning("equity_list_fetch_failed", error=scrub(str(exc))[:200])
        return {}
    if response.status_code != 200:
        log.warning("equity_list_http", status=response.status_code)
        return {}
    return parse_equity_list(response.text)


def yahoo_fetch_sync(symbols: list[str]) -> dict[str, dict[str, object]]:
    """Blocking Yahoo quote fetch (runs in a worker thread).

    Returns ``NSE symbol → quote row`` for the rows Yahoo answered. Imported
    lazily so a broken/absent curl_cffi degrades to the equity-list source
    instead of killing the script import.
    """
    from curl_cffi import requests as curl_requests  # noqa: PLC0415 — lazy by design

    session = curl_requests.Session(impersonate="chrome")
    try:
        try:
            session.get("https://fc.yahoo.com/", timeout=15)
        except Exception:  # noqa: BLE001 - connection reset is expected; cookies arrive
            pass
        crumb_response = session.get(YAHOO_CRUMB_URL, timeout=15)
        if crumb_response.status_code != 200 or not crumb_response.text.strip():
            raise RuntimeError(f"yahoo crumb http {crumb_response.status_code}")
        crumb = crumb_response.text.strip()

        out: dict[str, dict[str, object]] = {}
        for start in range(0, len(symbols), YAHOO_BATCH_SIZE):
            batch = symbols[start : start + YAHOO_BATCH_SIZE]
            yahoo_symbols = ",".join(f"{symbol}{YAHOO_SYMBOL_SUFFIX}" for symbol in batch)
            for attempt in range(2):
                response = session.get(
                    f"{YAHOO_QUOTE_URL}?symbols={yahoo_symbols}&crumb={crumb}",
                    timeout=20,
                )
                if response.status_code == 200:
                    break
                if attempt == 0:
                    time.sleep(YAHOO_PAUSE_SECONDS * 2)
            if response.status_code != 200:
                log.warning("yahoo_batch_http", status=response.status_code, size=len(batch))
                continue
            try:
                payload = response.json()
            except ValueError:
                log.warning("yahoo_batch_bad_json", size=len(batch))
                continue
            results = (payload.get("quoteResponse") or {}).get("result") or []
            for row in results:
                if not isinstance(row, dict):
                    continue
                quoted = str(row.get("symbol") or "")
                if quoted.endswith(YAHOO_SYMBOL_SUFFIX):
                    out[quoted[: -len(YAHOO_SYMBOL_SUFFIX)]] = row
            time.sleep(YAHOO_PAUSE_SECONDS)
        return out
    finally:
        session.close()


def extract_yahoo(
    rows: dict[str, dict[str, object]], symbol: str
) -> tuple[float | None, float | None]:
    """``(pe_ratio, market_cap_cr)`` from one Yahoo row; missing stays missing."""
    row = rows.get(symbol)
    if not row:
        return None, None
    pe = _positive(row.get("trailingPE"))
    raw_cap = _positive(row.get("marketCap"))
    market_cap_cr = None if raw_cap is None else raw_cap / RUPEES_PER_CRORE
    return pe, market_cap_cr


def _positive(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if result != result or result <= 0:  # NaN or non-positive
        return None
    return result


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    if not settings.database_url:
        log.error(
            "no_database", note="sync_profiles persists into PostgreSQL; DATABASE_URL is required"
        )
        return 1

    store = build_store(settings)
    await store.connect()
    try:
        universe = await store.list_universe(universe=settings.scanner.universe)
        by_symbol = {item.symbol.upper(): item for item in universe}
        if not universe:
            log.error("empty_universe", note="no mapped instruments to profile")
            return 1

        listing_by_symbol: dict[str, date] = {}
        if args.source in ("both", "equity-l"):
            async with httpx.AsyncClient(follow_redirects=True) as client:
                listing_by_symbol = await fetch_equity_list(client)
            covered = sum(1 for symbol in by_symbol if symbol in listing_by_symbol)
            log.info(
                "equity_list_loaded",
                rows=len(listing_by_symbol),
                universe_covered=covered,
                universe_total=len(by_symbol),
            )

        yahoo_rows: dict[str, dict[str, object]] = {}
        if args.source in ("both", "yahoo"):
            symbols = sorted(by_symbol)
            try:
                yahoo_rows = await asyncio.to_thread(yahoo_fetch_sync, symbols)
            except Exception as exc:  # noqa: BLE001 - the other source may still succeed
                log.warning("yahoo_failed", error=scrub(str(exc))[:200])
            log.info(
                "yahoo_loaded",
                rows=len(yahoo_rows),
                requested=len(symbols),
                batches=(len(symbols) + YAHOO_BATCH_SIZE - 1) // YAHOO_BATCH_SIZE,
            )

        if not listing_by_symbol and not yahoo_rows:
            log.error("no_profile_data", note="both sources returned nothing; nothing written")
            return 1

        profiles: list[StockProfile] = []
        stats: Counter[str] = Counter()
        for symbol, instrument in sorted(by_symbol.items()):
            listing = listing_by_symbol.get(symbol)
            pe, market_cap_cr = extract_yahoo(yahoo_rows, symbol)
            if listing is None and pe is None and market_cap_cr is None:
                stats["no_data"] += 1
                continue
            sources = []
            if pe is not None or market_cap_cr is not None:
                sources.append("yahoo")
            if listing is not None:
                sources.append("equity_l")
            profiles.append(
                StockProfile(
                    instrument_id=instrument.id,
                    pe_ratio=pe,
                    market_cap_cr=market_cap_cr,
                    listing_date=listing,
                    profile_source="+".join(sources),
                )
            )
            stats["pe" if pe is not None else "pe_missing"] += 1
            if market_cap_cr is not None:
                stats["market_cap"] += 1
            if listing is not None:
                stats["listing_date"] += 1

        if not args.dry_run:
            written = await store.upsert_stock_profiles(profiles)
        else:
            written = 0
        log.info(
            "sync_profiles_complete",
            universe=len(by_symbol),
            profiles=len(profiles),
            written=written,
            pe=stats["pe"],
            pe_missing=stats["pe_missing"],
            market_cap=stats["market_cap"],
            listing_date=stats["listing_date"],
            no_data=stats["no_data"],
            dry_run=bool(args.dry_run),
        )
        covered_pct = len(profiles) / len(by_symbol) * 100.0
        if covered_pct < args.min_coverage:
            log.error(
                "coverage_below_minimum",
                covered_pct=round(covered_pct, 2),
                required=args.min_coverage,
            )
            return 5
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scanner-sync-profiles", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--source",
        choices=("both", "yahoo", "equity-l"),
        default="both",
        help="which upstream(s) to fetch (default both)",
    )
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=80.0,
        help="exit non-zero when fewer than this %% of the universe got any profile data",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary: one clear line, no traceback
        log.error("sync_profiles_failed", error=scrub(str(exc))[:300])
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
