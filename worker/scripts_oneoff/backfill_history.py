"""One-off: pull a month of 1-minute candles for the F&O universe into a database.

Why this exists
---------------
The staging database is a clone of the live one, and a clone carries the candles that
existed when it was copied — one week, because that was the retention window. A month
of tape is what makes the instrument pages, the ORB board and a 5-minute chart worth
looking at on staging. Only 1-minute rows are fetched: the dashboard buckets 5-minute
bars out of `candles_1m` at read time (`app/lib/source-postgres.ts`), so storing a
second copy of the same tape at a coarser grain would create a table that can disagree
with the thing it is derived from.

The constraint that fixes the order of operations
-------------------------------------------------
`main.py::_retention_loop` deletes `candles_1m` and `indicator_snapshots` older than
`ops_settings['retention'].days` — at session start and every six hours after. With the
default of 7, a month fetched tonight is trimmed at the next tick. So retention is
raised **before** any request is spent, and the new value is read back from the table
rather than assumed.

Request shape
-------------
Angel's documented cap for ONE_MINUTE is 30 days per request, so the plan is one range
request per instrument, with per-day requests only for the days that range did not
serve. 212 requests instead of ~4,400, which matters: the budget is 5,000 historical
requests per hour shared with the live worker, and a per-day-first run would spend
almost all of it before the 09:15 warm-up.

Resumable: an instrument whose days are already covered is skipped, and the per-day
pass asks the table for exactly the (instrument, day) pairs still empty — so an
interrupted run costs the remainder, not the month. The one exception is that the
closed-day discovery below needs five instruments' answers before it can conclude, so
a resumed run spends five range requests (the first five symbols, which already hold
the month) and skips the remaining ~207.

One exception to that: the NSE holiday file this repository ships starts at 2026-10-20,
so every earlier closed session (2026-10-02 Gandhi Jayanti, for one) still counts as a
trading day here. The broker has no tape for it, and asking 212 instruments for it one
day at a time would spend 212 requests that can only return empty. So a day for which
no instrument's range answered is declared market-closed and dropped from the per-day
pass — and reported, so a genuinely missing day is visible rather than silently
forgiven.

Safety
------
This writes. It writes to whichever database `DATABASE_URL` names, so the identity of
that database is printed from the server itself (`current_database()` +
`inet_server_addr()`) and a non-private address is refused unless `--yes` is passed.
Two copies of this app exist on one machine and differ only by an env file.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import sys
import time
from datetime import date, datetime, timedelta

from worker.config import load_settings
from worker.logging_setup import configure_logging
from worker.marketdata.smartapi_auth import SmartApiAuth
from worker.marketdata.smartapi_rest import SmartApiRest
from worker.models import Candle
from worker.persistence.postgres import PostgresStore
from worker.timeutils import TradingCalendar, load_calendar, now_ist

#: A complete NSE session is 09:15→15:30, so ~374 one-minute bars. Zero-volume
#: minutes are omitted by the broker, so a thin name lands nearer 340. This number
#: is only used to judge whether a range answer is plausible, never to fabricate one.
BARS_PER_DAY = 340
#: The last 1-minute bar of a session is stamped 15:29; a day whose newest candle is
#: earlier was cut off mid-afternoon. Same threshold `prefetch_warmup` uses.
COMPLETE_AFTER = "15:25"
#: Both the dashboard (`RETENTION_MAX`) and the worker (`min(30, ...)`) clamp the
#: window at 30 days, so anything fetched near that edge is within one purge of
#: being deleted. 28 keeps the whole month inside the window it will be kept for.
MAX_DAYS = 28
#: A per-day fallback larger than this many requests is not spent blindly — it means
#: the range answers were wrong, and re-running the ranges is cheaper than guessing.
FALLBACK_REQUEST_BUDGET = 1200

#: The (instrument, day) pairs with no candle, for exactly the days the caller names.
#: One query for the whole universe: per-instrument round trips made the resumable
#: case slower than the fetch.
#:
#: `unnest(%s::date[])` and not `generate_series(%s::date, %s::date, interval '1 day')`
#: — there is no date/date/interval overload, so the latter silently resolves to
#: **timestamptz**: it returns datetime objects (which stop matching a `date` set in
#: Python) and it enumerates every calendar day, so weekends show up as gaps the
#: broker can never fill. Verified against the Mini on 2026-10-14: the first version
#: reported 1,908 "still empty" instrument-days, which is 212 symbols × (8 weekends +
#: 2026-10-02), none of them a real hole.
EMPTY_DAYS_SQL = """
SELECT i.id::int AS id, i.symbol, i.instrument_token, d.trading_date
FROM instruments i
CROSS JOIN unnest(%s::date[]) AS d(trading_date)
WHERE i.is_fno AND i.mapping_status = 'MAPPED' AND i.is_active
  AND i.instrument_token IS NOT NULL AND i.instrument_token <> ''
  AND NOT EXISTS (
      SELECT 1 FROM candles_1m c
      WHERE c.instrument_id = i.id AND c.trading_date = d.trading_date
  )
ORDER BY i.symbol, d.trading_date
"""

COVERED_DAYS_SQL = """
SELECT COUNT(DISTINCT trading_date)::int AS days
FROM candles_1m
WHERE instrument_id = %s AND trading_date BETWEEN %s AND %s
"""


def mask_url(url: str) -> str:
    """`scheme://user@host/database`, password removed. Never the whole string."""
    match = re.match(r"^(postgres(?:ql)?://)([^@/]*)(@)?(.*)$", url)
    if not match:
        return "(unrecognised connection string)"
    scheme, credentials, at, remainder = match.groups()
    if not at:
        return f"{scheme}•••@{remainder.split('?')[0]}"
    user = credentials.split(":")[0]
    return f"{scheme}{user}:•••@{remainder.split('?')[0]}"


def is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("/")[0]).is_private
    except ValueError:
        return False


async def database_identity(store: PostgresStore) -> tuple[str, str, str]:
    async with store._pool.connection() as conn, conn.transaction():
        row = await (
            await conn.execute(
                "SELECT current_database() AS db, coalesce(inet_server_addr()::text, '(not reported)') AS addr, now()::text AS now"
            )
        ).fetchone()
    return str(row["db"]), str(row["addr"]), str(row["now"])


async def read_retention(store: PostgresStore) -> int | None:
    value = await store.get_ops_setting("retention")
    if not value:
        return None
    try:
        return int(value.get("days"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def write_retention(store: PostgresStore, days: int) -> int | None:
    """Set the window the way the dashboard does, then read it back."""
    async with store._pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO ops_settings (key, value)
            VALUES ('retention', jsonb_build_object('days', %s::int))
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (days,),
        )
    return await read_retention(store)


def trading_days(calendar: TradingCalendar, first: date, last: date) -> list[date]:
    days: list[date] = []
    day = first
    while day <= last:
        if calendar.is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def in_window(candles: list[Candle], start: datetime, end: datetime) -> list[Candle]:
    return [candle for candle in candles if start <= candle.candle_start < end]


def by_day(candles: list[Candle]) -> dict[date, int]:
    counts: dict[date, int] = {}
    for candle in candles:
        counts[candle.trading_date] = counts.get(candle.trading_date, 0) + 1
    return counts


async def covered_days(store: PostgresStore, instrument_id: int, first: date, last: date) -> int:
    async with store._pool.connection() as conn, conn.transaction():
        row = await (await conn.execute(COVERED_DAYS_SQL, (instrument_id, first, last))).fetchone()
    return int(row["days"]) if row else 0


async def empty_days(store: PostgresStore, days: list[date]) -> list[tuple[int, str, str, date]]:
    """The (instrument, day) pairs still missing among `days` — as `date`, so the
    result stays comparable with the calendar's own trading-day set."""
    if not days:
        return []
    async with store._pool.connection() as conn, conn.transaction():
        rows = await (await conn.execute(EMPTY_DAYS_SQL, (days,))).fetchall()
    return [
        (int(row["id"]), str(row["symbol"]), str(row["instrument_token"]), row["trading_date"])
        for row in rows
    ]


async def store_candles(store: PostgresStore, candles: list[Candle]) -> int:
    """`upsert_candles` is one transaction per call; a month per instrument is too
    large a single transaction for a container with a shared disk.

    Deduplicated first, and that is not cosmetic: the upsert is
    `ON CONFLICT (instrument_id, candle_start_at) DO UPDATE`, and a batch that names
    the same key twice fails outright ("ON CONFLICT DO UPDATE command cannot be run")
    instead of writing — so one repeated timestamp from the broker would cost an
    instrument's whole month.
    """
    unique: dict[tuple[int, datetime], Candle] = {
        (candle.instrument_id, candle.candle_start): candle for candle in candles
    }
    rows = list(unique.values())
    written = 0
    for start in range(0, len(rows), 5000):
        written += await store.upsert_candles(rows[start : start + 5000])
    return written


async def range_or_split(
    rest: SmartApiRest, instrument_id: int, token: str, start: datetime, end: datetime
) -> tuple[list[Candle], int]:
    """One range request; if it answers empty, ask for the two halves.

    Empty full-day answers are a known behaviour of this endpoint (the same fallback
    exists in `prefetch_warmup.py` for exactly this reason), and a half-day that
    answers proves the data exists rather than that the symbol is dead.
    """
    candles = await rest.get_candles(
        instrument_id=instrument_id, symboltoken=token, interval="ONE_MINUTE", from_dt=start, to_dt=end
    )
    requests = 1
    bars = in_window(candles, start, end)
    if bars:
        return bars, requests
    middle = start + (end - start) / 2
    halves: list[Candle] = []
    for low, high in ((start, middle), (middle, end)):
        halves.extend(in_window(await rest.get_candles(
            instrument_id=instrument_id, symboltoken=token, interval="ONE_MINUTE", from_dt=low, to_dt=high
        ), start, end))
        requests += 1
    seen: set[datetime] = set()
    unique = [candle for candle in halves if not (candle.candle_start in seen or seen.add(candle.candle_start))]
    return unique, requests


async def report(store: PostgresStore, first: date, last: date) -> None:
    """Coverage read back out of the table, so what is claimed is what landed."""
    async with store._pool.connection() as conn, conn.transaction():
        rows = await (
            await conn.execute(
                """
                SELECT trading_date, COUNT(*)::int AS rows,
                       COUNT(DISTINCT instrument_id)::int AS instruments,
                       (MIN(candle_start_at) AT TIME ZONE 'Asia/Kolkata')::text AS first_bar,
                       (MAX(candle_start_at) AT TIME ZONE 'Asia/Kolkata')::text AS last_bar
                FROM candles_1m WHERE trading_date BETWEEN %s AND %s
                GROUP BY trading_date ORDER BY trading_date
                """,
                (first, last),
            )
        ).fetchall()
        total = await (
            await conn.execute(
                "SELECT COUNT(*)::int AS rows, pg_size_pretty(pg_total_relation_size('candles_1m')) AS size FROM candles_1m"
            )
        ).fetchone()
    print("\ncoverage in candles_1m", flush=True)
    for row in rows:
        print(
            f"  {row['trading_date']}  {row['rows']:>7} rows · {row['instruments']:>3} instruments · "
            f"{row['first_bar'][11:16]}–{row['last_bar'][11:16]}",
            flush=True,
        )
    print(
        f"  {sum(int(row['rows']) for row in rows):>7} rows in the window · table total {total['rows']} rows · {total['size']}",
        flush=True,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill 1-minute candles from the broker's history.")
    parser.add_argument("--days", type=int, default=MAX_DAYS, help=f"calendar days of history, max {MAX_DAYS}")
    parser.add_argument("--limit", type=int, default=0, help="stop after N instruments (0 = all)")
    parser.add_argument("--symbol", action="append", default=[], help="only these symbols (repeatable)")
    parser.add_argument("--probe", action="store_true", help="one instrument, one range request, no writes")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument("--keep-retention", action="store_true", help="refuse to raise retention (and refuse a window it would trim)")
    parser.add_argument("--yes", action="store_true", help="allow a non-private database address")
    args = parser.parse_args()

    days = max(1, min(int(args.days), MAX_DAYS))
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    store = PostgresStore(settings.database_url)
    await store.connect()
    auth = SmartApiAuth(settings=settings.angel)
    rest = SmartApiRest(settings=settings.angel, auth=auth)

    requests_made = 0
    started = time.monotonic()
    try:
        db, addr, server_now = await database_identity(store)
        print(f"database  {db} @ {addr}  (server clock {server_now})", flush=True)
        print(f"dsn       {mask_url(settings.database_url)}", flush=True)
        if not is_private(addr) and addr != "(not reported)" and not args.yes:
            print("\nrefusing: that is not a private address, so this is not this machine's own Postgres.", flush=True)
            print("Pass --yes only if writing to a hosted database is what you meant.", flush=True)
            return 1

        calendar = load_calendar()
        today = now_ist().date()
        first = today - timedelta(days=days)
        sessions = trading_days(calendar, first, today)
        if not sessions:
            print("no trading day in that window", flush=True)
            return 1
        print(f"window    {first} → {today} · {len(sessions)} trading days · {days}-day request span", flush=True)

        saved = await read_retention(store)
        label = "unset (worker default 7)" if saved is None else f"{saved} days"
        if saved is None or saved < days:
            if args.keep_retention:
                print(f"retention {label} — narrower than the window, and --keep-retention was passed. Aborting.", flush=True)
                return 1
            written = await write_retention(store, MAX_DAYS + 2)
            print(f"retention {label} → {written} days (read back from ops_settings)", flush=True)
            if written is None or written < days:
                print("retention was not accepted; aborting before a request is spent", flush=True)
                return 1
        else:
            print(f"retention {label} — already covers the window", flush=True)

        instruments = [i for i in await store.list_universe(universe="FNO") if i.instrument_token]
        wanted = {s.upper() for s in args.symbol}
        if wanted:
            instruments = [i for i in instruments if i.symbol.upper() in wanted]
        if args.limit:
            instruments = instruments[: args.limit]
        print(f"universe  {len(instruments)} mapped F&O instruments", flush=True)
        if not instruments:
            return 1

        await auth.get_session()
        print("broker    historical session open (reads only; no order endpoint is used)", flush=True)

        window_start = calendar.session(sessions[0]).open_at  # type: ignore[union-attr]
        last_session = calendar.session(sessions[-1])  # type: ignore[union-attr]
        window_end = min(now_ist(), last_session.close_at)
        if window_end <= window_start:
            print("the window has not opened yet; nothing to fetch", flush=True)
            return 1

        if args.probe:
            instrument = instruments[0]
            began = time.monotonic()
            bars, made = await range_or_split(rest, instrument.id, str(instrument.instrument_token), window_start, window_end)
            requests_made += made
            counts = by_day(bars)
            print(
                f"\nprobe     {instrument.symbol} · {len(bars)} bars · {made} request(s) · {time.monotonic() - began:.1f}s\n"
                f"          days covered {len(counts)}/{len(sessions)} · per-day "
                f"{min(counts.values()) if counts else 0}–{max(counts.values()) if counts else 0}\n"
                f"          span {bars[0].candle_start if bars else '-'} → {bars[-1].candle_start if bars else '-'}\n"
                f"          thin days: {', '.join(f'{d}:{n}' for d, n in sorted(counts.items()) if n < BARS_PER_DAY) or 'none'}",
                flush=True,
            )
            print("probe writes nothing.", flush=True)
            return 0

        stored = 0
        from_range = 0
        skipped: list[str] = []
        thin: list[str] = []
        failed: list[str] = []
        # Which calendar days the broker actually has tape for, counted across the
        # instruments whose ranges answered. A day missing from every one of them is
        # a market holiday the local calendar file does not list (it starts at
        # 2026-10-20, so 2026-10-02 Gandhi Jayanti counts as a session here), not a
        # fetch failure — and chasing it per instrument would cost 212 requests that
        # can only ever come back empty.
        day_hits: dict[date, int] = {}
        dead_days: set[date] = set()
        for position, instrument in enumerate(instruments, start=1):
            expected = [day for day in sessions if day not in dead_days]
            already = await covered_days(store, instrument.id, sessions[0], sessions[-1])
            if already >= len(expected):
                skipped.append(instrument.symbol)
                continue
            try:
                bars, made = await range_or_split(rest, instrument.id, str(instrument.instrument_token), window_start, window_end)
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the month
                failed.append(f"{instrument.symbol}: {type(exc).__name__}")
                continue
            requests_made += made
            counts = by_day(bars)
            if bars:
                if not args.dry_run:
                    stored += await store_candles(store, bars)
                else:
                    stored += len(bars)
                from_range += 1
                for day in counts:
                    day_hits[day] = day_hits.get(day, 0) + 1
                # Only after several instruments have served a full range can their
                # shared absence be read as "the market was shut".
                if from_range >= 5:
                    dead_days = {day for day in sessions if day not in day_hits}
            short = [day for day in expected if day not in counts]
            if short:
                thin.append(f"{instrument.symbol}({len(counts)})")
            if position % 20 == 0 or position == len(instruments):
                elapsed = (time.monotonic() - started) / 60
                print(
                    f"  [{position:>3}/{len(instruments)}] {instrument.symbol:<14} · {stored} bars · "
                    f"{requests_made} requests · {elapsed:.1f} min",
                    flush=True,
                )

        print(
            f"\nranges    {from_range} instruments served · {len(skipped)} already complete · "
            f"{len(thin)} short: {', '.join(thin[:20]) or 'none'}",
            flush=True,
        )
        if dead_days:
            print(
                f"          no instrument has tape for {', '.join(str(d) for d in sorted(dead_days))} — "
                "closed session the calendar file does not list; not fetched per day",
                flush=True,
            )

        # Per-day pass: exactly the pairs the ranges left empty among the days the
        # market was demonstrably open, restricted to the instruments this run was
        # asked to handle. Without the scope filter a `--limit 3` rehearsal would
        # report the other 209 symbols' untouched days as failures of the range pass.
        in_scope = {instrument.id for instrument in instruments}
        wanted_days = [day for day in sessions if day not in dead_days]
        pairs = [row for row in await empty_days(store, wanted_days) if row[0] in in_scope]
        if wanted:
            pairs = [row for row in pairs if row[1].upper() in wanted]
        print(f"per-day   {len(pairs)} instrument-days still empty", flush=True)
        if len(pairs) > FALLBACK_REQUEST_BUDGET:
            print(
                f"          more than the {FALLBACK_REQUEST_BUDGET}-request fallback budget — refusing to spend it blindly.\n"
                "          That usually means the range answers were empty, not that the symbols are dead.\n"
                "          Re-run with --probe, or pass a narrower --days.",
                flush=True,
            )
        elif pairs:
            for position, (instrument_id, symbol, token, trading_date) in enumerate(pairs, start=1):
                session = calendar.session(trading_date)
                if session is None:
                    continue
                try:
                    bars, made = await range_or_split(rest, instrument_id, token, session.open_at, session.close_at)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{symbol}@{trading_date}: {type(exc).__name__}")
                    continue
                requests_made += made
                if bars and not args.dry_run:
                    stored += await store_candles(store, bars)
                elif bars:
                    stored += len(bars)
                if position % 50 == 0:
                    print(f"  {position}/{len(pairs)} instrument-days · {stored} bars · {requests_made} requests", flush=True)

        elapsed = (time.monotonic() - started) / 60
        print(
            f"\ndone      {stored} bars {'(dry run, nothing written)' if args.dry_run else 'written'} · "
            f"{requests_made} historical requests in {elapsed:.1f} min "
            f"({requests_made / max(elapsed, 0.1):.0f}/min against 120/min, 5000/hour)",
            flush=True,
        )
        if failed:
            print(f"failed    {len(failed)}: {', '.join(failed[:15])}", flush=True)
        await report(store, sessions[0], sessions[-1])
        return 1 if failed else 0
    finally:
        await rest.close()
        await auth.close()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
