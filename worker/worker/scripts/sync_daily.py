"""Refresh the daily/weekly bars and the RSI + price-extreme columns.

Usage::

    python -m worker.scripts.sync_daily                  # nightly tail refresh
    python -m worker.scripts.sync_daily --full           # deep backfill (once,
                                                         # + corporate-action repair)
    python -m worker.scripts.sync_daily --symbols RELIANCE,TCS
    python -m worker.scripts.sync_daily --dry-run        # compute, write nothing

What this populates, and from what:

* ``candles_1d`` — ONE_DAY bars from Angel's historical API. That history is
  **corporate-action adjusted** (verified 2026-10-12: no cliffs across the
  2018 TCS bonus or the 2017 RELIANCE bonus), which is exactly what an RSI or
  a 52-week high must be computed on;
* ``candles_1w`` — ISO weeks resampled from the daily rows, with the newest row
  allowed to be the still-forming week;
* ``stock_details`` technicals — daily/weekly RSI-14, the two-timeframe bias,
  52-week and all-time extremes, and ``data_start`` (the extremes are labeled
  "all time" in the UI but actually reach back to the broker's history depth,
  which the column makes explicit).

Two fetch modes, deliberately different:

* **nightly (default)** re-fetches only the trailing ``--tail-days`` window
  (400 calendar days ≈ 57 weeks). That covers everything the 52-week columns
  and a 14-period weekly RSI read, and it re-fetches it *every night* so an
  adjustment inside the window heals itself;
* **``--full``** walks the whole history from ``--since`` in ~1900-day windows
  (the API's 2000-day cap for ONE_DAY, minus margin). This is the one-time
  backfill and the repair path for a corporate action that rewrites history
  older than the tail window. An instrument with **no stored rows at all** is
  fetched fully on its first sighting, so new universe additions seed deep
  without an operator remembering to.

Writes are idempotent upserts; the run is resumable by re-running it. Windows
that come back empty are retried once (the broker occasionally returns an
empty set for a range that has data — same trap as ``scripts_oneoff/
prefetch_warmup.py``), but an empty *young-history* window is legitimate for a
recent listing, so emptiness is never on its own a failure. A failure is the
tail window coming back empty when the database does not already hold bars
inside it — that instrument's technicals are then left untouched rather than
recomputed from stale data.

Run it after market close: it competes with the live worker for the broker's
3/sec / 120/min historical quota, and it wants the day's completed bar.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from ..analytics.rsi import (
    ENTRY,
    NEUTRAL,
    OVERBOUGHT,
    classify,
    resample_and_compute,
    week_start_of,
    wilder_rsi,
)
from ..config import WorkerSettings, load_settings
from ..logging_setup import configure_logging, get_logger, scrub
from ..marketdata.smartapi_auth import SmartApiAuth
from ..marketdata.smartapi_rest import SmartApiRest
from ..models import DailyCandle, Instrument, StockTechnicals, WeeklyCandle
from ..persistence.memory import InMemoryStore
from ..persistence.postgres import PostgresStore
from ..persistence.store import ScannerStore
from ..timeutils import ist_zoneinfo, now_ist

log = get_logger(__name__)

#: Angel's history for NSE equities effectively begins here (probed 2026-10-12;
#: older requests return nothing). The ATH/ATL columns carry this as their
#: documented depth via stock_details.data_start.
DEFAULT_SINCE = date(2002, 1, 1)
#: ONE_DAY allows 2000 calendar days per request; 1900 keeps margin.
WINDOW_DAYS = 1900
#: Nightly refresh window: ≥ 52 weeks, so 52W high/low always reflect the
#: current tape even if a corporate action adjusted history inside it.
DEFAULT_TAIL_DAYS = 400
RETRY_PAUSE_SECONDS = 3.0


def build_store(settings: WorkerSettings) -> ScannerStore:
    return PostgresStore(settings.database_url) if settings.database_url else InMemoryStore()


def midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ist_zoneinfo())


def windows_between(
    start: date, end: date, *, chunk_days: int = WINDOW_DAYS
) -> list[tuple[date, date]]:
    """Inclusive date windows ``[start, end]`` in ≤ ``chunk_days`` slices."""
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=chunk_days - 1), end)
        out.append((cursor, last))
        cursor = last + timedelta(days=1)
    return out


async def fetch_window(
    rest: SmartApiRest, instrument: Instrument, start: date, end: date
) -> tuple[list[DailyCandle], bool]:
    """One ONE_DAY request (+1 retry). Returns the bars and whether it was empty twice.

    ``end`` is inclusive on the exchange calendar; the request pads one day and
    the result is filtered client-side because Angel does not document whether
    ``todate`` is inclusive for daily bars.
    """
    to_dt = midnight(end + timedelta(days=1))
    for attempt in range(2):
        raw = await rest.get_candles(
            instrument_id=instrument.id,
            symboltoken=instrument.instrument_token,
            interval="ONE_DAY",
            from_dt=midnight(start),
            to_dt=to_dt,
        )
        bars = [
            DailyCandle(
                instrument_id=instrument.id,
                trading_date=candle.trading_date,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
            for candle in raw
            if start <= candle.trading_date <= end
        ]
        if bars:
            return bars, False
        if attempt == 0:
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
    return [], True


async def sync_instrument(
    rest: SmartApiRest,
    store: ScannerStore,
    instrument: Instrument,
    *,
    full: bool,
    since: date,
    tail_days: int,
    pace: float,
    dry_run: bool,
    today: date,
) -> tuple[str, dict[str, object]]:
    """Refresh one instrument. Returns ``("ok"|"unchanged"|"failed", info)``."""
    stored = await store.load_daily_candles(instrument.id)
    stored_newest = stored[-1].trading_date if stored else None
    stored_by_date = {bar.trading_date: bar for bar in stored}

    fetched: dict[date, DailyCandle] = {}
    final_window_empty = False
    deep = full or stored_newest is None
    if deep:
        # Deep walk (first sighting, or an explicit repair run).
        for start, end in windows_between(since, today):
            bars, empty = await fetch_window(rest, instrument, start, end)
            final_window_empty = empty
            for bar in bars:
                fetched[bar.trading_date] = bar
            await asyncio.sleep(pace)
        write_from = since
    else:
        # Nightly tail refresh. Snapped to a Monday so an adjusted week is
        # replaced whole, never stitched from old and new adjustment factors.
        tail_start = week_start_of(stored_newest - timedelta(days=tail_days))
        if tail_start > today:
            return "unchanged", {"reason": "stored_newest_in_future"}
        bars, final_window_empty = await fetch_window(rest, instrument, tail_start, today)
        for bar in bars:
            fetched[bar.trading_date] = bar
        await asyncio.sleep(pace)
        write_from = tail_start

    # An empty fetch is tolerated when the database already holds bars fresher
    # than the tail floor — those nights the run is "unchanged", not failed.
    freshness_floor = today - timedelta(days=tail_days)
    covered_by_storage = stored_newest is not None and stored_newest >= freshness_floor
    if final_window_empty and not covered_by_storage:
        return "failed", {"empty_windows": 1}
    if not fetched:
        return "unchanged", {"covered_by_storage": True}

    merged = dict(stored_by_date)
    merged.update(fetched)
    ordered = [merged[key] for key in sorted(merged)]

    series = wilder_rsi([bar.close for bar in ordered])
    rsi_by_date = dict(zip([bar.trading_date for bar in ordered], series, strict=True))
    weekly, weekly_series = resample_and_compute(ordered)
    daily_rsi = series[-1] if series else None
    weekly_rsi = weekly_series[-1] if weekly_series else None
    bias = classify(daily_rsi, weekly_rsi)

    newest = ordered[-1].trading_date
    window_52w = [bar for bar in ordered if bar.trading_date > newest - timedelta(days=365)]
    technicals = StockTechnicals(
        instrument_id=instrument.id,
        daily_rsi=daily_rsi,
        weekly_rsi=weekly_rsi,
        rsi_bias=bias,
        rsi_as_of=newest,
        data_start=ordered[0].trading_date,
        high_52w=max(bar.high for bar in window_52w),
        low_52w=min(bar.low for bar in window_52w),
        ath=max(bar.high for bar in ordered),
        atl=min(bar.low for bar in ordered),
    )

    fetched_rows = [
        replace(bar, rsi14=rsi_by_date.get(bar.trading_date)) for bar in fetched.values()
    ]
    weekly_floor = week_start_of(write_from) if write_from else None
    weekly_rows = [
        WeeklyCandle(
            instrument_id=instrument.id,
            week_start=week.week_start,
            open=week.open,
            high=week.high,
            low=week.low,
            close=week.close,
            volume=week.volume,
            rsi14=weekly_series[index],
        )
        for index, week in enumerate(weekly)
        if weekly_floor is None or week.week_start >= weekly_floor
    ]

    if not dry_run:
        await store.upsert_daily_candles(fetched_rows)
        await store.upsert_weekly_candles(weekly_rows)
        await store.update_stock_technicals([technicals])

    return "ok", {
        "fetched": len(fetched_rows),
        "weeks_written": len(weekly_rows),
        "bars_total": len(ordered),
        "daily_rsi": None if daily_rsi is None else round(daily_rsi, 2),
        "weekly_rsi": None if weekly_rsi is None else round(weekly_rsi, 2),
        "bias": bias,
        "high_52w": technicals.high_52w,
        "low_52w": technicals.low_52w,
    }


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    if not settings.database_url:
        log.error(
            "no_database", note="sync_daily persists into PostgreSQL; DATABASE_URL is required"
        )
        return 1
    if not settings.angel.configured:
        log.error("no_angel_credentials", note="ANGEL_* credentials are required to fetch history")
        return 1

    store = build_store(settings)
    await store.connect()
    auth = SmartApiAuth(settings=settings.angel)
    rest = SmartApiRest(settings=settings.angel, auth=auth)
    try:
        universe = await store.list_universe(universe=settings.scanner.universe)
        if args.symbols:
            wanted = {
                symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
            }
            universe = [item for item in universe if item.symbol.upper() in wanted]
        if args.limit:
            universe = universe[: args.limit]
        if not universe:
            log.error("empty_universe", note="no mapped instruments to sync")
            return 1
        usable = [item for item in universe if item.instrument_token]
        if len(usable) != len(universe):
            log.warning(
                "instruments_without_token",
                skipped=len(universe) - len(usable),
            )

        await auth.get_session()  # fail fast on credential problems

        since = date.fromisoformat(args.since)
        today = now_ist().date()
        eta_minutes = len(usable) * args.pace / 60.0
        if args.full:
            approx_requests = len(usable) * len(windows_between(since, today))
            eta_minutes = approx_requests * args.pace / 60.0
            log.info(
                "full_backfill_start",
                instruments=len(usable),
                since=str(since),
                approx_requests=approx_requests,
                eta_minutes=round(eta_minutes, 1),
            )
        else:
            log.info(
                "tail_refresh_start",
                instruments=len(usable),
                tail_days=args.tail_days,
                eta_minutes=round(eta_minutes, 1),
            )

        outcomes = Counter()
        biases: Counter[str] = Counter()
        failures: list[str] = []
        written_daily = 0
        written_weekly = 0
        for position, instrument in enumerate(usable, start=1):
            try:
                outcome, info = await sync_instrument(
                    rest,
                    store,
                    instrument,
                    full=args.full,
                    since=since,
                    tail_days=args.tail_days,
                    pace=args.pace,
                    dry_run=args.dry_run,
                    today=today,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not end the run
                outcome, info = (
                    "failed",
                    {"error": f"{type(exc).__name__}: {scrub(str(exc))[:120]}"},
                )
            outcomes[outcome] += 1
            if outcome == "ok":
                written_daily += int(info.get("fetched", 0))
                written_weekly += int(info.get("weeks_written", 0))
                biases[str(info.get("bias"))] += 1
                log.info(
                    "synced",
                    progress=f"{position}/{len(usable)}",
                    symbol=instrument.symbol,
                    **info,
                )
            elif outcome == "failed":
                failures.append(instrument.symbol)
                log.warning("sync_failed_symbol", symbol=instrument.symbol, **info)
            else:
                log.info("unchanged", symbol=instrument.symbol, **info)

        failed_pct = len(failures) / len(usable) * 100.0
        log.info(
            "sync_daily_complete",
            instruments=len(usable),
            ok=outcomes["ok"],
            unchanged=outcomes["unchanged"],
            failed=outcomes["failed"],
            failed_pct=round(failed_pct, 2),
            daily_rows_written=written_daily,
            weekly_rows_written=written_weekly,
            bias_entry=biases[ENTRY],
            bias_overbought=biases[OVERBOUGHT],
            bias_neutral=biases[NEUTRAL],
            bias_not_evaluable=biases["None"],
            dry_run=bool(args.dry_run),
        )
        if failures:
            log.warning("failed_symbols", count=len(failures), symbols=failures[:20])
        if failed_pct > 100.0 - args.min_success:
            log.error(
                "success_below_minimum",
                failed_pct=round(failed_pct, 2),
                required_success=args.min_success,
            )
            return 5
        return 0
    finally:
        await rest.close()
        await auth.close()
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanner-sync-daily", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--full",
        action="store_true",
        help="deep-walk the whole history since --since (one-time backfill and "
        "corporate-action repair); default is the nightly tail refresh",
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE.isoformat(),
        help=f"earliest date for deep walks (default {DEFAULT_SINCE})",
    )
    parser.add_argument(
        "--tail-days",
        type=int,
        default=DEFAULT_TAIL_DAYS,
        help="nightly refresh window in calendar days (default 400)",
    )
    parser.add_argument("--symbols", help="comma-separated subset (e.g. RELIANCE,TCS)")
    parser.add_argument("--limit", type=int, help="sync only the first N instruments")
    parser.add_argument("--pace", type=float, default=1.05, help="seconds between broker requests")
    parser.add_argument("--dry-run", action="store_true", help="fetch and compute, write nothing")
    parser.add_argument(
        "--min-success",
        type=float,
        default=90.0,
        help="exit non-zero when fewer than this % of instruments synced cleanly",
    )
    args = parser.parse_args(argv)
    if args.tail_days > 2000:
        parser.error("--tail-days must stay under the API's 2000-day cap")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary: one clear line, no traceback
        log.error("sync_daily_failed", error=scrub(str(exc))[:300])
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
