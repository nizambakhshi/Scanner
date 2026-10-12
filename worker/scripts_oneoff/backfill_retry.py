"""Pass 2: re-fetch instruments whose day is incomplete, paced slower.

"Incomplete" is judged by the last candle's stamp, not by a row count: the
broker omits zero-volume minutes, so a sparse but complete day has few rows,
while an instrument that stopped at 13:24 (when the morning feed died) has a
late-morning last stamp. Refetching overwrites whole-day broker rows via
upsert, so an already-complete instrument is left untouched by this query.

Two traps this script hit on its first real run (2026-10-12), kept here as
warnings:

* The completeness threshold must be built as an IST timestamp and converted
  with `AT TIME ZONE 'Asia/Kolkata'`. Writing `… + interval '5.5 hours'`
  instead compares against a naive timestamp resolved in the *session* time
  zone — UTC on Supabase — which puts the bar at 21:05 UTC, unreachable by
  any candle of the day, and flags all 212 instruments as incomplete.
* The broker's historical API occasionally returns an empty set for a
  full-day request (observed on KAYNES/DIXON while a half-day window for the
  same token returned 145 bars). An empty full-day answer is therefore
  retried as two half-day windows before the symbol is given up on.
"""

import asyncio
import sys

from worker.config import load_settings
from worker.logging_setup import configure_logging
from worker.marketdata.smartapi_auth import SmartApiAuth
from worker.marketdata.smartapi_rest import SmartApiRest
from worker.persistence.postgres import PostgresStore
from worker.timeutils import load_calendar, now_ist

PACE_SECONDS = 1.05
#: The last 1-minute bar of an NSE session is stamped 15:29; anything whose
#: newest candle sits before this was cut off mid-afternoon.
COMPLETE_AFTER = "15:25"


async def incomplete_instruments(store: PostgresStore, trading_date) -> list[tuple[int, str, str]]:
    async with store._pool.connection() as conn:
        rows = await (
            await conn.execute(
                f"""
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
                """,
                (trading_date, trading_date),
            )
        ).fetchall()
    return [(int(row["id"]), row["symbol"], str(row["instrument_token"])) for row in rows]


async def fetch_one(rest: SmartApiRest, window, instrument_id: int, symbol: str, token: str) -> int:
    def fetch(from_dt, to_dt):
        return rest.get_candles(
            instrument_id=instrument_id,
            symboltoken=token,
            interval="ONE_MINUTE",
            from_dt=from_dt,
            to_dt=to_dt,
        )

    candles = await fetch(window.open_at, window.close_at)
    today = [c for c in candles if window.open_at <= c.candle_start < window.close_at]
    if not today:
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
        today = [c for c in [*first, *second]
                 if not (c.candle_start in seen or seen.add(c.candle_start))]
        if today:
            print(f"  {symbol}: full-day empty, split windows yielded {len(today)}", flush=True)
    if today:
        await store_upsert(today)
        return len(today)
    return 0


store: PostgresStore


async def store_upsert(candles) -> None:
    await store.upsert_candles(candles)


async def main() -> int:
    global store
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    store = PostgresStore(settings.database_url)
    await store.connect()
    auth = SmartApiAuth(settings=settings.angel)
    rest = SmartApiRest(settings=settings.angel, auth=auth)
    try:
        await auth.get_session()
        window = load_calendar().session(now_ist().date())

        for pace in (PACE_SECONDS, PACE_SECONDS * 2):
            pending = await incomplete_instruments(store, window.trading_date)
            if not pending:
                break
            print(f"pass pace={pace}s · missing={len(pending)}", flush=True)
            stored = 0
            for instrument_id, symbol, token in pending:
                try:
                    stored += await fetch_one(rest, window, instrument_id, symbol, token)
                except Exception as exc:  # noqa: BLE001
                    print(f"  error {symbol}: {type(exc).__name__}", flush=True)
                await asyncio.sleep(pace)
            print(f"  stored {stored} candles", flush=True)

        final = await incomplete_instruments(store, window.trading_date)
        print(f"still missing: {len(final)}" + (f" — {', '.join(s for _, s, _ in final)}" if final else ""), flush=True)
        return 0 if not final else 1
    finally:
        await rest.close()
        await auth.close()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
