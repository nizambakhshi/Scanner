"""One-off: after-hours backfill of today's completed 1-minute candles.

The morning live run died mid-session (13:24), so every instrument is missing
the 13:25–15:29 tail. Broker history is the authoritative OHLCV for a closed
session, so this overwrites today's rows wholesale with what the broker
reports — same parser the warmup uses (row_to_candle), same rate limiter
(120 historical requests/min, 3/sec).
"""

import asyncio
import sys

from worker.config import load_settings
from worker.logging_setup import configure_logging
from worker.marketdata.smartapi_auth import SmartApiAuth
from worker.marketdata.smartapi_rest import SmartApiRest
from worker.persistence.postgres import PostgresStore
from worker.timeutils import load_calendar, now_ist


async def count_today(store: PostgresStore, trading_date) -> int:
    async with store._pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT COUNT(*)::int AS n FROM candles_1m WHERE trading_date = %s",
                (trading_date,),
            )
        ).fetchone()
    return int(row["n"])


async def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    store = PostgresStore(settings.database_url)
    await store.connect()
    auth = SmartApiAuth(settings=settings.angel)
    rest = SmartApiRest(settings=settings.angel, auth=auth)
    try:
        await auth.get_session()
        print("login ok", flush=True)
        window = load_calendar().session(now_ist().date())
        instruments = await store.list_universe(universe="FNO")
        print(f"universe {len(instruments)} · window {window.open_at:%H:%M}–{window.close_at:%H:%M}", flush=True)
        before = await count_today(store, window.trading_date)

        stored = 0
        fetched = 0
        empty: list[str] = []
        failed: list[str] = []
        for instrument in instruments:
            if not instrument.instrument_token:
                continue
            try:
                candles = await rest.get_candles(
                    instrument_id=instrument.id,
                    symboltoken=instrument.instrument_token,
                    interval="ONE_MINUTE",
                    from_dt=window.open_at,
                    to_dt=window.close_at,
                )
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the batch
                failed.append(f"{instrument.symbol}: {type(exc).__name__}")
                continue
            today = [
                candle
                for candle in candles
                if window.open_at <= candle.candle_start < window.close_at
            ]
            if not today:
                empty.append(instrument.symbol)
                continue
            await store.upsert_candles(today)
            fetched += 1
            stored += len(today)

        after = await count_today(store, window.trading_date)
        print(
            f"done: instruments_fetched={fetched}/{len(instruments)} "
            f"candles_stored={stored} rows_today {before}→{after}",
            flush=True,
        )
        if empty:
            print(f"no_rows ({len(empty)}): {', '.join(empty[:20])}", flush=True)
        if failed:
            print(f"failed ({len(failed)}): {', '.join(failed[:20])}", flush=True)
        return 1 if failed and fetched == 0 else 0
    finally:
        await rest.close()
        await auth.close()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
