"""Pre-store the warm-up window: the last HISTORICAL_WARMUP_DAYS of 1m candles.

Warm-up is DB-first (``pipeline.warm_up`` → ``load_completed_candles``), and the
indicators need only the trailing ``warmup_candles_required + 10`` bars, so a
complete latest day already satisfies the next session — the broker fetch at the
open is a *fallback* for whatever the DB cannot serve. This script stores the
full window anyway, for the operator-facing reasons:

* the dashboard's instrument pages render 400 candles; a week of tape gives the
  detail view real context instead of a single day;
* insurance: if the latest day is ever purged or the warm-up requirement grows,
  the history is already in the table and the open-time fallback stays idle.

The window mirrors ``_warm_from_rest`` exactly — trading days in
``(next_session − HISTORICAL_WARMUP_DAYS, today)`` — so what lands in the table
is what the fallback would have asked the broker for. Non-trading days are
skipped, so only real sessions cost requests. Rows are upserted, and an
(instrument, day) pair whose last candle is at or after 15:25 IST is not
refetched, which makes the run resumable.

Two traps carried over from ``backfill_retry.py`` (which hit them on 2026-10-12):

* completeness is compared against ``(%s::date + time '15:25') AT TIME ZONE
  'Asia/Kolkata'`` — a naive ``+ interval '5.5 hours'`` resolves in the session
  time zone (UTC on Supabase) and flags every instrument incomplete;
* the broker's historical API occasionally returns an empty set for a full-day
  request that clearly has data. An empty full-day answer is retried as two
  half-day windows before the pair is given up on.
"""

import asyncio
import sys
from datetime import date, timedelta

from worker.config import load_settings
from worker.logging_setup import configure_logging
from worker.marketdata.smartapi_auth import SmartApiAuth
from worker.marketdata.smartapi_rest import SmartApiRest
from worker.persistence.postgres import PostgresStore
from worker.timeutils import TradingCalendar, load_calendar, now_ist

PACE_SECONDS = 1.05
#: The last 1-minute bar of an NSE session is stamped 15:29; a day whose newest
#: candle sits before this was cut off mid-afternoon.
COMPLETE_AFTER = "15:25"

INCOMPLETE_SQL = f"""
SELECT i.id::int AS id, i.symbol, i.instrument_token
FROM instruments i
WHERE i.is_fno AND i.mapping_status = 'MAPPED' AND i.is_active
  AND i.instrument_token IS NOT NULL AND i.instrument_token <> ''
  AND COALESCE(
      (SELECT MAX(c.candle_start_at) FROM candles_1m c
       WHERE c.instrument_id = i.id AND c.trading_date = %s),
      'epoch')
      < ((%s::date + time '{COMPLETE_AFTER}') AT TIME ZONE 'Asia/Kolkata')
ORDER BY i.symbol
"""


async def incomplete_for_day(
    store: PostgresStore, trading_date: date
) -> list[tuple[int, str, str]]:
    async with store._pool.connection() as conn:
        rows = await (
            await conn.execute(INCOMPLETE_SQL, (trading_date, trading_date))
        ).fetchall()
    return [(int(row["id"]), row["symbol"], str(row["instrument_token"])) for row in rows]


def next_trading_day(calendar: TradingCalendar, start: date) -> date:
    day = start
    for _ in range(10):
        if calendar.is_trading_day(day):
            return day
        day += timedelta(days=1)
    raise SystemExit("no trading day found within 10 days of the anchor")


async def fetch_one(
    rest: SmartApiRest,
    store: PostgresStore,
    window,
    instrument_id: int,
    symbol: str,
    token: str,
) -> int:
    def fetch(from_dt, to_dt):
        return rest.get_candles(
            instrument_id=instrument_id,
            symboltoken=token,
            interval="ONE_MINUTE",
            from_dt=from_dt,
            to_dt=to_dt,
        )

    candles = await fetch(window.open_at, window.close_at)
    day = [c for c in candles if window.open_at <= c.candle_start < window.close_at]
    if not day:
        # Empty full-day answers have been observed for tokens that clearly
        # have data (see module docstring). Split the window in half; if the
        # broker serves the halves, merge and store them.
        middle = window.open_at + (window.close_at - window.open_at) / 2
        await asyncio.sleep(0.35)
        first = [c for c in await fetch(window.open_at, middle)
                 if window.open_at <= c.candle_start < window.close_at]
        await asyncio.sleep(0.35)
        second = [c for c in await fetch(middle, window.close_at)
                  if window.open_at <= c.candle_start < window.close_at]
        seen: set = set()
        day = [c for c in [*first, *second]
               if not (c.candle_start in seen or seen.add(c.candle_start))]
        if day:
            print(f"  {symbol}: full-day empty, split windows yielded {len(day)}", flush=True)
    if day:
        await store.upsert_candles(day)
        return len(day)
    return 0


async def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    store = PostgresStore(settings.database_url)
    await store.connect()
    auth = SmartApiAuth(settings=settings.angel)
    rest = SmartApiRest(settings=settings.angel, auth=auth)
    try:
        await auth.get_session()
        calendar = load_calendar()
        warmup_days = settings.angel.historical_warmup_days
        anchor = next_trading_day(calendar, now_ist().date() + timedelta(days=1))
        first = anchor - timedelta(days=warmup_days)
        last = now_ist().date()
        days: list[date] = []
        day = first
        while day <= last:
            if calendar.is_trading_day(day):
                days.append(day)
            day += timedelta(days=1)
        print(
            f"warmup window: {first} → {last} · trading days {len(days)} "
            f"({', '.join(str(d) for d in days)}) · anchor session {anchor}",
            flush=True,
        )

        for pace in (PACE_SECONDS, PACE_SECONDS * 2):
            pending: list[tuple[date, int, str, str]] = []
            for trading_date in days:
                for instrument_id, symbol, token in await incomplete_for_day(store, trading_date):
                    pending.append((trading_date, instrument_id, symbol, token))
            if not pending:
                break
            eta = len(pending) * pace / 60.0
            print(
                f"pass pace={pace}s · instrument-days missing={len(pending)} · ~{eta:.0f} min",
                flush=True,
            )
            stored = 0
            for trading_date, instrument_id, symbol, token in pending:
                window = calendar.session(trading_date)
                if window is None:
                    continue
                try:
                    stored += await fetch_one(rest, store, window, instrument_id, symbol, token)
                except Exception as exc:  # noqa: BLE001 - keep the run going, count misses at the end
                    print(f"  error {symbol} {trading_date}: {type(exc).__name__}", flush=True)
                await asyncio.sleep(pace)
            print(f"  stored {stored} candles", flush=True)

        still_missing: list[str] = []
        for trading_date in days:
            gaps = await incomplete_for_day(store, trading_date)
            print(f"  {trading_date}: complete" if not gaps else f"  {trading_date}: {len(gaps)} incomplete")
            still_missing.extend(f"{symbol}@{trading_date}" for _, symbol, _ in gaps)
        if still_missing:
            print(f"still missing: {', '.join(still_missing)}", flush=True)
            return 1
        print("warmup window fully stored", flush=True)
        return 0
    finally:
        await rest.close()
        await auth.close()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
